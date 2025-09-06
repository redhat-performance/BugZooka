import logging

try:
    import xmltodict  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    xmltodict = None  # Fallback for environments without xmltodict

from bugzooka.core.utils import extract_prow_test_phase, extract_prow_test_name

logger = logging.getLogger(__name__)


def load_xml_as_dict(xml_path):
    """
    Loads xml as a dictionary.

    :param xml_path: xml file path
    :return: xml to dictionary
    """
    if xmltodict is None:
        raise ImportError(
            "xmltodict is required to parse XML files. Please install it to use XML parsing features."
        )
    with open(xml_path, "r", encoding="utf-8") as f:
        return xmltodict.parse(f.read())


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


def summarize_junit_operator_xml(xml_path):
    """
    Summarize junit_operator xml file.

    :param xml_path: xml file path
    :return: (phase, test_name, failure_message)
    """
    test_phase = None
    test_name = None
    failure_message = None
    try:
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
    except Exception as e:
        logger.error("Error parsing junit_operator.xml file: %s", e)
        return None, None, None
