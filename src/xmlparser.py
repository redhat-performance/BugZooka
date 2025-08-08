import re
import xmltodict
from src.utils import (
    extract_prow_test_phase, 
    extract_prow_test_name
)


def load_xml_as_dict(xml_path):
    """
    Loads xml as a dictionary.

    :param xml_path: xml file path
    :return: xml to dictionary
    """
    with open(xml_path, "r", encoding="utf-8") as f:
        return xmltodict.parse(f.read())


def extract_orion_changepoint_context(failure_text):
    """
    Extracts changepoints.

    :param failure_text: failures xml text
    :return: chanepoints string
    """
    lines = failure_text.strip().splitlines()
    changepoint_idx = -1

    # Find the changepoint
    for i, line in enumerate(lines):
        if "-- changepoint" in line:
            changepoint_idx = i
            break

    if changepoint_idx != -1:
        parts = lines[changepoint_idx].split("|")
        return f"{parts[-2].strip()} % changepoint --- {re.sub(r'X+-X+', 'ocp-qe-perfscale', parts[4].strip(), count=1)}"
    else:
        return "No changepoint found."


def get_failing_test_cases(xml_path):
    """
    Yield each failing test case from a given XML path.

    :param xml_path: xml file path
    :return: None
    """
    xml_root = load_xml_as_dict(xml_path)
    for testsuite in xml_root.values():
        if "testsuite" not in testsuite:
            continue

        ts = testsuite["testsuite"]
        if "@failures" not in ts or int(ts["@failures"]) <= 0:
            continue

        for each_case in ts["testcase"]:
            if "failure" in each_case:
                yield each_case


def summarize_orion_xml(xml_path):
    """
    Summarize a given xml file.

    :param xml_path: xml file path
    :return: summary of the xml file
    """
    for each_case in get_failing_test_cases(xml_path):
        failure_output = each_case["failure"]
        changepoint_str = extract_orion_changepoint_context(failure_output)
        if changepoint_str != "No changepoint found.":
            return f"\n--- Test Case: {each_case['@name']} --- " + changepoint_str
    return ""


def summarize_junit_operator_xml(xml_path):
    """
    Summarize junit_operator xml file.

    :param xml_path: xml file path
    :return: (phase, test_name, failure_message)
    """
    test_phase = None
    test_name = None
    failure_message = None
    for case in get_failing_test_cases(xml_path):
        case_name = case["@name"]

        # Assign phase if not already found
        if not test_phase:
            test_phase = extract_prow_test_phase(case_name)

        # Assign failure message once phase is known
        if not failure_message and test_phase:
            failure_message = case["failure"].get("#text")

        # Assign test name once
        if not test_name:
            test_name = extract_prow_test_name(case_name)

        # If all found, no need to keep looping
        if test_phase and test_name and failure_message:
            break
    return test_phase, test_name, failure_message
