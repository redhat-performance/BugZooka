import logging
import os
import re
import subprocess
from collections import deque
import requests

from src.constants import MAX_CONTEXT_SIZE
from src.prompts import ERROR_SUMMARIZATION_PROMPT
from src.utils import (
    download_file_from_gcs,
    filter_most_frequent_errors,
    list_gcs_files,
    run_shell_command,
)

logger = logging.getLogger(__name__)


def download_prow_build_log(gcs_path, output_dir):
    """
    Download the build-log.txt file.

    :param gcs_path: path in gcs storage
    :param output_dir: output directory to store artifacts
    :return: None
    """
    file_url = f"gs://{gcs_path}/build-log.txt"
    download_file_from_gcs(file_url, output_dir)


def get_prow_inner_artifact_files(gcs_path):
    """
    Given a GCS path to a Prow job, return the list of files inside the nested log folder
    under 'artifacts/' (e.g. artifacts/<log_folder>/*).

    :param gcs_path: path in gcs storage
    :return: a tuple of gcs folder path and the inner files
    """
    artifact_path = f"gs://{gcs_path}/artifacts/"
    top_files = list_gcs_files(artifact_path)

    # Identify nested log folder (match last segment with gcs_path)
    log_folder = next(
        (
            f.strip("/").split("/")[-1]
            for f in top_files
            if f.strip("/").split("/")[-1] in gcs_path
        ),
        None,
    )
    if not log_folder:
        logger.info("No matching log folder found.")
        return None, []

    log_folder_path = f"{artifact_path}{log_folder}/"
    inner_files = list_gcs_files(log_folder_path)
    return log_folder_path, inner_files


def download_prow_orion_xmls(gcs_path, output_dir):
    """
    Downloads all orion xmls to the output directory.

    :param gcs_path: path in gcs storage
    :param output_dir: output directory to store artifacts
    :return: None
    """
    try:
        log_folder_path, inner_files = get_prow_inner_artifact_files(gcs_path)
        if not log_folder_path:
            return

        orion_folders = [
            f.strip("/").split("/")[-1] for f in inner_files if "orion" in f
        ]

        orion_xmls = []
        for folder in orion_folders:
            xml_path = f"{log_folder_path}{folder}/artifacts/"
            xml_files = list_gcs_files(xml_path)
            orion_xmls.extend(f for f in xml_files if f.endswith(".xml"))

        for xml_url in orion_xmls:
            download_file_from_gcs(xml_url, output_dir)

    except subprocess.CalledProcessError as e:
        logger.error("Error processing Orion XMLs: %s", e.stderr)


def download_prow_cluster_operators(gcs_path, output_dir):
    """
    Downloads prow clusteroperators.json file.

    :param gcs_path: path in gcs storage
    :param output_dir: output directory to store artifacts
    :return: None
    """
    try:
        log_folder_path, _ = get_prow_inner_artifact_files(gcs_path)
        if not log_folder_path:
            return
        download_file_from_gcs(
            f"{log_folder_path}gather-extra/artifacts/clusteroperators.json", output_dir
        )
    except subprocess.CalledProcessError as e:
        logger.error("Error downloading clusteroperators.json: %s", e.stderr)


def download_prow_logs(url, output_dir="/tmp/"):
    """
    Extract GCS path from the URL and download build logs and orion XMLs.

    :param url: prow logs url
    :param output_dir: output directory to store artifacts
    :return: log directory
    """
    match = re.search(r"/(\d+)$", url)
    if not match:
        raise ValueError("Invalid URL format: Cannot extract build ID")

    build_id = match.group(1)

    if "view/gs/" not in url:
        raise ValueError("Invalid Prow URL: GCS path not found.")

    gcs_path = url.split("view/gs/")[1]

    log_dir = os.path.join(output_dir, build_id)
    orion_dir = os.path.join(log_dir, "orion")

    os.makedirs(orion_dir, exist_ok=True)

    download_prow_build_log(gcs_path, log_dir)
    download_prow_cluster_operators(gcs_path, log_dir)
    download_prow_orion_xmls(gcs_path, orion_dir)

    return log_dir


def get_logjuicer_extract(directory_path, job_name):
    """Extracts erros using logjuicer using fallback mechanism.

    :param directory_path: path of output directory
    :param job name: job name for the failure
    :return: a list of errors
    """
    file_path = os.path.join(directory_path, f"{job_name}.txt")
    url = f"https://raw.githubusercontent.com/vishnuchalla/ocp-qe-prow-build-logs/main/{job_name}.txt"

    try:
        logger.info("Attempting to download log file from: %s", url)
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(response.text)
        logger.info("Downloaded and saved to: %s", file_path)
        try:
            full_errors_cmd = f"logjuicer diff {file_path} {directory_path}/build-log.txt | cut -d '|' -f2- | grep -i -e 'error' -e 'failure' -e 'exception' -e 'fatal' -e 'panic'"
            full_errors = run_shell_command(full_errors_cmd)
        except Exception as e:
            logger.error("Failed to execute logjuicer full errors command: %s", e)
            return None
        try:
            frequent_errors_cmd = f"logjuicer diff {file_path} {directory_path}/build-log.txt | cut -d '|' -f2- | logmine | grep -i -e 'error' -e 'failure' -e 'exception' -e 'fatal' -e 'panic'"
            frequent_errors = run_shell_command(frequent_errors_cmd)
            return filter_most_frequent_errors(full_errors, frequent_errors)
        except Exception as e:
            logger.error("Failed to execute logjuicer frequent errors command: %s", e)
            return full_errors
    except requests.exceptions.RequestException as e:
        logger.error("Failed to fetch log file: %s", e)
        return None


def get_logmine_extract(directory_path):
    """
    Extracts errors using logmine with a fallback mechanism.

    :param directory_path: path of output directory
    :return: a list of errors
    """
    try:
        full_errors_cmd = f"cat {directory_path}/build-log.txt | cut -d '|' -f2- | grep -i -e 'error' -e 'failure' -e 'exception' -e 'fatal' -e 'panic'"
        full_errors = run_shell_command(full_errors_cmd)
    except Exception as e:
        logger.error("Failed to execute full errors command: %s", e)
        return None
    try:
        frequent_errors_cmd = f"cat {directory_path}/build-log.txt | cut -d '|' -f2- | logmine | grep -i -e 'error' -e 'failure' -e 'exception' -e 'fatal' -e 'panic'"
        frequent_errors = run_shell_command(frequent_errors_cmd)
        return filter_most_frequent_errors(full_errors, frequent_errors)
    except Exception as e:
        logger.error("Failed to execute logmine frequent errors command: %s", e)
        return full_errors


def search_prow_errors(directory_path, job_name):
    """
    Extracts errors by using multiple mechanisms.

    :param directory_path: path of output directory
    :param directory_path: job name for the failure
    :return: a list of errors
    """
    logjuicer_extract = get_logjuicer_extract(directory_path, job_name)
    if logjuicer_extract is None:
        return get_logmine_extract(directory_path)
    return logjuicer_extract


def download_url_to_log(url, log_file_path):
    """
    Downloads the content from a given URL and writes it to a log file.

    :param url: url of the job
    :param log_file_path: log file path
    :return: output directory
    """
    output_dir = "/tmp"
    log_file_path = output_dir + log_file_path

    logger.info("Creating a file %s", log_file_path)
    try:
        response = requests.get(url, stream=True, verify=False, timeout=30)
        response.raise_for_status()
        with open(log_file_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=MAX_CONTEXT_SIZE):
                file.write(chunk)
        logger.info("Successfully downloaded content from %s to %s", url, log_file_path)

    except requests.exceptions.RequestException as e:
        logger.error("Error downloading from %s: %s", url, e)
    except Exception as e:
        logger.error("An error occurred: %s", e)
    return output_dir


def search_errors_in_file(file_path, context_lines=3):
    """
    Opens a log file and searches for error terms while capturing context.

    :param file_path: The path of the log file to search for errors
    :param context_lines: Number of lines to include before and after the error
    :return: A list of error contexts, each containing surrounding lines
    """
    error_keywords = ["error", "failure", "exception", "fatal", "panic"]
    error_contexts = []
    previous_lines = deque(maxlen=context_lines)

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            log_lines = f.readlines()

        for i, line in enumerate(log_lines):
            if any(keyword in line.lower() for keyword in error_keywords):
                # Capture context lines before and after the error
                start = max(0, i - context_lines)
                end = min(len(log_lines), i + context_lines + 1)
                error_snippet = log_lines[start:end]
                error_contexts.append("".join(error_snippet).strip())

            previous_lines.append(line)

        if error_contexts:
            logger.info(
                "Found %s errors with context in %s:", len(error_contexts), file_path
            )
            return error_contexts

        logger.info("No errors found in %s.", file_path)
        return []

    except Exception as e:
        logger.error("Error opening the file %s: %s", file_path, e)
        return []


def generate_prompt(error_list):
    """
    Generates a structured prompt for the LLM to analyze relevant error logs.

    :param error_list: list of errors
    :return: prompt messages
    """
    # Convert to messages list format
    messages = [
        {"role": "system", "content": ERROR_SUMMARIZATION_PROMPT["system"]},
        {
            "role": "user",
            "content": ERROR_SUMMARIZATION_PROMPT["user"].format(
                error_list="\n".join(error_list)[:MAX_CONTEXT_SIZE]
            ),
        },
        {"role": "assistant", "content": ERROR_SUMMARIZATION_PROMPT["assistant"]},
    ]
    return messages
