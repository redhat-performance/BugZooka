import logging
import re
import subprocess
from src.constants import TOP_N_ERRROS

logger = logging.getLogger(__name__)


def extract_job_details(text):
    """
    Extract the name and hyperlink (URL).

    :param text: message text in slack
    :return: job link and the job name
    """
    try:
        URL_PATTERN = re.compile(r"(https://[^\s|]+)")
        url_match = URL_PATTERN.search(text)
        name_match = re.search(r"Job\s+\*?(.+?)\*?\s+ended", text)
        if url_match and name_match:
            return url_match.group(0), name_match.group(1)
        return None, None
    except Exception as e:
        logger.error("Failure in extracting job details: %s", e)
        return None, None


def run_shell_command(command):
    """
    Run a shell command and return the output lines.

    :param command: shell command to execute
    :return: command output
    """
    logger.info("Executing: %s", command)
    result = subprocess.run(
        command, shell=True, check=True, capture_output=True, text=True
    )
    return result.stdout.strip().splitlines()


def list_gcs_files(gcs_path):
    """
    List files in a GCS path.

    :param gcs_path: gcs path to list artifacts
    :return: list of artifacts
    """
    command = f"gsutil ls {gcs_path}"
    return run_shell_command(command)

def extract_prow_test_phase(case_name):
    """
    Identifies the prow test phase.

    :param case_name: name of the case
    :return: phase
    """
    for keyword in ("pre", "post", "test"):
        if f"{keyword} phase" in case_name:
            return keyword
    return None


def categorize_prow_failure(step_name, step_phase):
    """
    Categorize prow failures.

    :param step_name: step name
    :param step_phase: step phase
    :return: categorized preview tag message
    """
    failure_map = {
        "provision": "provision failure",
        "deprovision": "deprovision failure",
        "gather": "must gather failure",
        "orion": "change point detection failure",
        "cerberus": "cerberus health check failure",
        "node-readiness": "nodes readiness check failure",
        "openshift-qe": "workload failure",
        "upgrade": "upgrade failure"
    }

    for keyword, description in failure_map.items():
        if keyword in step_name:
            return f"{step_phase} phase: {description}"

    return f"{step_phase} phase: {step_name} step failure"


def extract_prow_test_name(case_name):
    """
    Extracts prow test name from a given string.

    :param case_name: name of the case
    :return: name of the test
    """
    match = re.search(r'(?<=-\s)(.+?)(?=\s+container)', case_name)
    return match.group(1) if match else None


def download_file_from_gcs(gcs_url, local_path):
    """
    Download a single file from GCS.

    :param gcs_url: gcs path to list artifacts
    :param local_path: local file system path to download
    :return: None
    """
    command = f"gsutil -m cp -r {gcs_url} {local_path}"
    file_name = gcs_url.strip("/").split("/")[-1]
    try:
        logger.info("Downloading %s...", file_name)
        subprocess.run(command, shell=True, check=True)
        logger.info("%s downloaded successfully.", file_name)
    except subprocess.CalledProcessError as e:
        logger.error("Error downloading %s: %s", file_name, e)


def filter_most_frequent_errors(full_errors, frequent_errors):
    """
    Filters out the most recent errors from a full list.

    :param full_errors: full list of errors
    :param frequent_errors: frequent list of errors
    :return: most significant set of errors
    """
    frequency_map = {}
    for entry in frequent_errors:
        trimmed = entry.strip()
        parts = trimmed.split(" ", 1)
        if len(parts) == 2:
            frequency, message = parts
            if int(frequency) not in frequency_map:
                frequency_map[int(frequency)] = [message]
            else:
                frequency_map[int(frequency)].append(message)

    sorted_freqs = sorted(frequency_map.items(), key=lambda x: -x[0])

    top_errors_set = set()
    for _, errors in sorted_freqs:
        for e in errors:
            top_errors_set.add(e)
            if len(top_errors_set) >= TOP_N_ERRROS:
                break
        if len(top_errors_set) >= TOP_N_ERRROS:
            break
    top_error_patterns = [re.compile(re.escape(err)) for err in top_errors_set]
    top_errors_from_full = [
        e
        for e in full_errors
        if any(pattern.search(e) for pattern in top_error_patterns)
    ]
    return top_errors_from_full


def get_slack_message_blocks(markdown_header, preformatted_text):
    """
    Prepares a slack message building blocks

    :param markdown_header: markdown header to be displayed
    :param preformatted_text: preformatted text message
    :return: a sanitized version of text blocks
    """
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": markdown_header}},
        {
            "type": "rich_text",
            "block_id": "error_logs_block",
            "elements": [
                {
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": preformatted_text.strip()}],
                    "border": 0,
                }
            ],
        },
    ]
