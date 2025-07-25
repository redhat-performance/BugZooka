import xmltodict


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
    header_line = None

    # Find the header and changepoint
    for i, line in enumerate(lines):
        if "uuid" in line and "timestamp" in line:
            header_line = (
                f"| {'idx':<3} | {'uuid':<4} | {'timestamp':<10} | {'buildUrl':<10} | "
                f"{'metric':<10} | {'is_changepoint':<15} | {'percentage_change':<20} |"
            )
        if "-- changepoint" in line:
            changepoint_idx = i
            break

    # Extract the lines: header, two before, the changepoint, and two after
    context = []
    if changepoint_idx != -1:
        start = max(0, changepoint_idx - 2)
        end = min(len(lines), changepoint_idx + 3)
        context_lines = lines[start:end]

        if header_line:
            context.append("Header:\n" + header_line)
        context.append("\nChangepoint Context:")
        context.extend(context_lines)

    return "\n".join(context) if context else "No changepoint found."


def summarize_orion_xml(xml_path):
    """
    Summarize a given xml file.

    :param xml_path: xml file path
    :return: summary of the xml file
    """
    xml_root = load_xml_as_dict(xml_path)
    for testsuite in xml_root.values():
        if "testsuite" not in testsuite:
            continue

        ts = testsuite["testsuite"]
        if "@failures" not in ts or int(ts["@failures"]) <= 0:
            continue

        for each_case in ts["testcase"]:
            if "failure" not in each_case:
                continue

            failure_output = each_case["failure"]
            changepoint_str = extract_orion_changepoint_context(failure_output)
            if changepoint_str == "No changepoint found.":
                return ""

            return f"\n--- Test Case: {each_case['@name']} ---\n" + changepoint_str

    return ""
