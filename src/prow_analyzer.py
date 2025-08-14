import json
import logging
import os
import re
from collections import deque
from pathlib import Path
from src.constants import BUILD_LOG_TAIL, MAINTENANCE_ISSUE
from src.log_summarizer import search_prow_errors
from src.utils import categorize_prow_failure
from src.xmlparser import (
    summarize_orion_xml,
    summarize_junit_operator_xml
)

logger = logging.getLogger(__name__)


def get_cluster_operator_errors(directory_path):
    """
    Extracts errors from the clusteroperators.json.

    :param directory_path: directory path for the artifacts
    :return: list of errors
    """
    try:
        with open(
            f"{directory_path}/clusteroperators.json", "r", encoding="utf-8"
        ) as f:
            cluster_operators_data = json.load(f)
        err_conditions = []
        for each_item in cluster_operators_data["items"]:
            each_dict = {"Name": each_item["metadata"]["name"]}
            for condition in each_item["status"]["conditions"]:
                if (
                    condition["type"] == "Degraded" and condition["status"] == "True"
                ) or (
                    condition["type"] == "Available" and condition["status"] == "False"
                ):
                    each_dict["Status"] = condition["status"]
                    each_dict["Reason"] = condition["reason"]
                    each_dict["Message"] = condition["message"]
                    err_conditions.append(json.dumps(each_dict))
        return err_conditions
    except Exception as e:
        logger.error("Failed to fetch log file: %s", e)
        return []


def scan_orion_xmls(directory_path):
    """
    Extracts errors from orion xmls.

    :param directory_path: directory path for the artifacts
    :return: list of errors
    """
    base_dir = Path(f"{directory_path}/orion")
    xml_files = base_dir.glob("*.xml")
    for xml_file in xml_files:
        xml_content = summarize_orion_xml(xml_file)
        if xml_content != "":
            return [xml_content]
    return []


def analyze_prow_artifacts(directory_path, job_name):
    """
    Analyzes prow artifacts and extracts errors.

    :param directory_path: directory path for the artifacts
    :param job_name: job name to base line with
    :return: tuple of (list of errors, categorization_message, requires_llm, is_install_issue)
    """
    step_summary = ""
    categorization_message = ""
    pattern = re.compile(r"Logs for container test in pod .*")
    timestamp_strip = re.compile(r"^\x1b\[[0-9;]*m\w*\x1b\[0m\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\s*")
    build_file_path = os.path.join(directory_path, "build-log.txt")
    if not os.path.isfile(build_file_path):
        return [
            "Prow maintanence issues, couldn't even find the build-log.txt file"
        ], MAINTENANCE_ISSUE, False, True
    with open(build_file_path, "r", errors="replace", encoding="utf-8") as f:
        matched_line = next(
            (
                timestamp_strip.sub("", line).strip()
                for line in f
                if pattern.search(timestamp_strip.sub("", line))
            ),
            None
        )
        if matched_line is None:
            matched_line = (
                "Couldn't identify the failure step, likely a maintanence issue"
            )
            return [matched_line], MAINTENANCE_ISSUE, False, True
    junit_operator_file_path = os.path.join(directory_path, "junit_operator.xml")
    if os.path.isfile(junit_operator_file_path):
        step_phase, step_name, step_summary = summarize_junit_operator_xml(junit_operator_file_path)
        if step_name and step_phase:
            categorization_message = categorize_prow_failure(step_name, step_phase)
        else:
            categorization_message = categorize_prow_failure(matched_line, "unknown")
            step_summary = ""
    cluster_operators_file_path = os.path.join(directory_path, "clusteroperators.json")
    if not os.path.isfile(cluster_operators_file_path):
        with open(build_file_path, "r", errors="replace", encoding="utf-8") as f:
            build_log_content = list(deque(f, maxlen=BUILD_LOG_TAIL))
        return [
            "\n Somehow couldn't find clusteroperators.json file",
            matched_line + "\n",
            step_summary + "\n".join(build_log_content),
        ], categorization_message, False, False
    cluster_operator_errors = get_cluster_operator_errors(directory_path)
    if len(cluster_operator_errors) == 0:
        orion_errors = scan_orion_xmls(directory_path)
        if len(orion_errors) == 0:
            return [matched_line] + [step_summary] + search_prow_errors(directory_path, job_name), categorization_message, True, False
        return [matched_line + "\n"] + orion_errors, categorization_message, False, False
    return [matched_line + "\n"] + cluster_operator_errors, categorization_message, False, False
