ERROR_SUMMARIZATION_PROMPT = {
    "system": "You are an AI assistant specializing in analyzing logs to detect failures.",
    "user": "I have scanned log files and found potential error logs. Here is the list:\n\n{error_list}\n\n"
    "Analyze these errors further and return the most critical erorrs or failures.\nYour response should be in plain text.",
    "assistant": "Sure! Here are the most relevant logs:",
}

ERROR_FILTER_PROMPT = {
    "system": "You are an AI assistant specializing in analyzing logs to detect failures.",
    "user": "I have scanned log files and found potential error logs. Here is the list:\n\n{error_list}\n\n"
    "Analyze these errors and return only the **top 5 most critical errors** based on severity, frequency, and impact. "
    "Ensure that your response contains a **diverse set of failures** rather than redundant occurrences of the same error.\n"
    "Respond **only** with a valid JSON list containing exactly 5 error messages, without any additional explanation.\n"
    "Example response format:\n"
    '["Error 1 description", "Error 2 description", "Error 3 description", "Error 4 description", "Error 5 description"]',
    "assistant": "[]",
}

GENERIC_APP_PROMPT = {
    "system": "You are an expert in diagnosing and troubleshooting application failures, logs, and errors. "
    "Your task is to analyze log summaries from various applications, identify the root cause, "
    "and suggest relevant fixes based on best practices. "
    "Focus on application-specific failures rather than infrastructure or environment issues.",
    "user": "Here is a log summary from an application failure:\n\n{error_summary}\n\n"
    "Based on this summary, provide a structured breakdown of:\n"
    "- The failing component or service\n"
    "- The probable root cause of the failure\n"
    "- Steps to reproduce or verify the issue\n"
    "- Suggested resolution, including configuration changes, code fixes, or best practices.",
    "assistant": "**Failing Component:** <Identified service or component>\n\n"
    "**Probable Root Cause:** <Describe why the failure occurred>\n\n"
    "**Verification Steps:**\n"
    "- <Step 1>\n"
    "- <Step 2>\n"
    "- <Step 3>\n\n"
    "**Suggested Resolution:**\n"
    "- <Code fixes or configuration updates>\n"
    "- <Relevant logs, metrics, or monitoring tools>",
}
