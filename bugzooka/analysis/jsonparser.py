import re
import json
import logging

logger = logging.getLogger(__name__)


def extract_json_changepoints(json_data):
    """
    Extract changepoints from JSON changepoint summaries.

    :param json_data: List of changepoint records
    :return: list of changepoint strings
    """
    changepoints = []
    for entry in json_data:
        if not entry.get("is_changepoint", False):
            continue

        build_url = entry.get("buildUrl", "N/A")
        metrics = entry.get("metrics", {})

        for metric_name, metric_data in metrics.items():
            percentage = metric_data.get("percentage_change", 0)
            if percentage != 0:  # only flag actual changepoints
                label_string = metric_data.get("labels", "")
                url = re.sub(r"X+-X+", "ocp-qe-perfscale", build_url.strip(), count=1)
                changepoints.append(
                    f"{label_string} {metric_name} regression detection --- {percentage} % changepoint --- {url}"
                )

    return changepoints


def summarize_orion_json(json_path):
    """
    Summarize a given json file.

    :param json_path: json file path
    :return: summary of the json file
    """
    with open(json_path, "r") as f:
        json_data = json.load(f)
    summaries = []
    changepoints = extract_json_changepoints(json_data)
    for entry in json_data:
        if entry.get("is_changepoint", False):
            for cp in changepoints:
                summaries.append(f"\n--- Test Case: {cp} ---")
    return "".join(summaries)
