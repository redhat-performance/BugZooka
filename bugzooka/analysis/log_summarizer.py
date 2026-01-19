import logging
import os
import re
import subprocess
from typing import List, Tuple, Optional
import requests

from bugzooka.core.constants import MAX_CONTEXT_SIZE
from bugzooka.analysis.prompts import ERROR_SUMMARIZATION_PROMPT
from bugzooka.analysis.failure_keywords import FAILURE_KEYWORDS
from bugzooka.core.utils import (
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


def download_prow_junit_operator_xml(gcs_path, output_dir):
    """
    Download the junit_operator.xml file.

    :param gcs_path: path in gcs storage
    :param output_dir: output directory to store artifacts
    :return: None
    """
    file_url = f"gs://{gcs_path}/artifacts/junit_operator.xml"
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
    download_prow_junit_operator_xml(gcs_path, log_dir)
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
    url = f"https://raw.githubusercontent.com/redhat-performance/ocp-qe-prow-build-logs/main/{job_name}.txt"

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


def classify_failure_type(errors_list, categorization_message, is_install_issue):
    """
    Map analysis outputs to a display label for failure type.
    """
    cat = (categorization_message or "").lower()
    try:
        for keyword, (label, _) in FAILURE_KEYWORDS.items():
            if keyword in cat:
                return label

        if is_install_issue:
            return "Install"

        # Heuristic
        if any('"Name"' in e and '"Reason"' in e for e in (errors_list or [])):
            return "Install"

        if cat.strip():
            return "Prow Other"
        return "Unknown"
    except Exception:
        return "Unknown"


def render_failure_breakdown(
    counts,
    total_jobs,
    total_failures,
    version_counts=None,
    version_type_counts=None,
    version_type_messages=None,
):
    """
    Build a polished markdown summary from counts using the labels returned by classifier.
    """
    if total_jobs == 0:
        return "No job messages found in the selected period."

    separator = "---------------------------------------------------------------"

    failure_rate = (total_failures / total_jobs) * 100.0 if total_jobs else 0
    lines = [
        f"• **Total Jobs:** {total_jobs}",
        f"• **Failures:** {total_failures} ({failure_rate:.0f}% failure rate)",
    ]
    lines.append(f"    {separator}")
    return "\n".join(lines)


def render_version_breakdown(
    version,
    total_failures,
    version_counts,
    version_type_counts,
    version_type_messages=None,
):
    """
    Build markdown text for a single OpenShift version, including nested type counts
    and optionally the original messages.
    """
    count = version_counts.get(version, 0)
    pct = (count / total_failures) * 100 if total_failures else 0
    mini_separator = "---"

    lines = [f"**{version}** — {count} ({pct:.0f}% )"]

    types_for_version = (
        version_type_counts.get(version, {}) if version_type_counts else {}
    )
    for t, t_count in sorted(types_for_version.items(), key=lambda x: -x[1]):
        t_pct = (t_count / count) * 100 if count else 0
        lines.append(f":white_small_square: {t} — {t_count} ({t_pct:.0f}% )")
        if version_type_messages and version in version_type_messages:
            for i, msg in enumerate(
                version_type_messages.get(version, {}).get(t, []), start=1
            ):
                lines.append(f":black_small_square::black_small_square: {msg}")

    lines.append(f"   {mini_separator}")
    return "\n".join(lines)


def render_type_breakdown(counts, total_failures):
    """
    Build markdown text for Failure Breakdown by the type of Issue.
    """
    separator = "---------------------------------------------------------------"
    lines = [":construction: **Failure Breakdown by the type of Issue:**", ""]
    sorted_types = sorted(counts.items(), key=lambda x: -x[1])
    for ftype, count in sorted_types:
        pct = (count / total_failures) * 100 if total_failures else 0
        lines.append(f"• **{ftype}** — {count} ({pct:.0f}% )")
    lines.append(f"    {separator}")
    return "\n".join(lines)


def build_summary_sections(
    counts,
    total_jobs: int,
    total_failures: int,
    version_counts: Optional[dict] = None,
    version_type_counts: Optional[dict] = None,
    version_type_messages: Optional[dict] = None,
    verbose: bool = False,
) -> List[Tuple[str, str]]:
    """
    Build a list of (markdown_header, content_text) sections for Slack posting.
    This function decides whether to chunk by versions to avoid Slack's msg_too_long.
    """
    sections: List[Tuple[str, str]] = []

    summary_text = render_failure_breakdown(
        counts,
        total_jobs,
        total_failures,
        version_counts=version_counts,
        version_type_counts=version_type_counts,
        version_type_messages=version_type_messages if verbose else None,
    )

    MAX_SLACK_TEXT = 3500

    # Precompute type breakdown once
    type_text = render_type_breakdown(counts, total_failures)

    # Helper to generate version breakdown text blocks (sorted newest first)
    def _generate_version_texts() -> List[str]:
        if not version_counts:
            return []

        def _version_key(v: str):
            try:
                major, minor = v.split(".")
                return (int(major), int(minor))
            except Exception:
                return (0, 0)

        texts: List[str] = []
        for v in sorted(version_counts.keys(), key=_version_key, reverse=True):
            texts.append(
                render_version_breakdown(
                    v,
                    total_failures,
                    version_counts or {},
                    version_type_counts or {},
                    version_type_messages if verbose else None,
                )
            )
        return texts

    if version_counts and len(summary_text) > MAX_SLACK_TEXT:
        # Compact header with type breakdown
        header_lines = summary_text.splitlines()
        header_body = "\n".join(header_lines[:6])
        header_body = f"{header_body}\n\n{type_text}"
        sections.append((":star: *Summary* :star:\n", header_body))

        # Per-version sections (one message per version)
        for version_text in _generate_version_texts():
            sections.append(
                (":label: *Failure Breakdown by OpenShift Version*\n", version_text)
            )
    else:
        # Ensure type breakdown appears in the main summary message
        body = f":star: *Summary* :star:\n\n{summary_text}\n\n{type_text}"
        sections.append(
            ("", body)
        )  # header can be empty, content contains full summary

        # When versions exist, include a single combined version section
        version_texts = _generate_version_texts()
        if version_texts:
            versions_body = "\n".join(version_texts)
            sections.append(
                (":label: *Failure Breakdown by OpenShift Version*\n", versions_body)
            )

    return sections
