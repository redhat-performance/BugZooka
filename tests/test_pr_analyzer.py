"""
Tests for PR analysis request parsing and output sanitization.
"""
from bugzooka.analysis.pr_analyzer import _parse_pr_request, _sanitize_gemini_output


class TestParsePrRequest:
    """Tests for _parse_pr_request() — single and multi-PR parsing."""

    def test_single_pr(self):
        text = "analyze pr: https://github.com/openshift/ovn-kubernetes/pull/3169, compare with 5.0"
        result = _parse_pr_request(text)
        assert result == ("openshift", "ovn-kubernetes", ["3169"], "5.0")

    def test_multi_pr_space_separated(self):
        text = (
            "analyze pr: https://github.com/openshift/ovn-kubernetes/pull/3169 "
            "https://github.com/openshift/ovn-kubernetes/pull/3170, compare with 5.0"
        )
        result = _parse_pr_request(text)
        assert result == ("openshift", "ovn-kubernetes", ["3169", "3170"], "5.0")

    def test_multi_pr_three_prs(self):
        text = (
            "analyze pr: https://github.com/openshift/ovn-kubernetes/pull/100 "
            "https://github.com/openshift/ovn-kubernetes/pull/200 "
            "https://github.com/openshift/ovn-kubernetes/pull/300, compare with 4.19"
        )
        result = _parse_pr_request(text)
        assert result == ("openshift", "ovn-kubernetes", ["100", "200", "300"], "4.19")

    def test_mixed_repos_returns_none(self):
        text = (
            "analyze pr: https://github.com/openshift/ovn-kubernetes/pull/3169 "
            "https://github.com/openshift/installer/pull/100, compare with 5.0"
        )
        assert _parse_pr_request(text) is None

    def test_mixed_orgs_returns_none(self):
        text = (
            "analyze pr: https://github.com/openshift/ovn-kubernetes/pull/3169 "
            "https://github.com/other-org/ovn-kubernetes/pull/100, compare with 5.0"
        )
        assert _parse_pr_request(text) is None

    def test_no_version_returns_none(self):
        text = "analyze pr: https://github.com/openshift/ovn-kubernetes/pull/3169"
        assert _parse_pr_request(text) is None

    def test_no_url_returns_none(self):
        text = "analyze pr: some random text, compare with 5.0"
        assert _parse_pr_request(text) is None

    def test_version_case_insensitive(self):
        text = "analyze pr: https://github.com/openshift/ovn-kubernetes/pull/3169, Compare With 4.19"
        result = _parse_pr_request(text)
        assert result == ("openshift", "ovn-kubernetes", ["3169"], "4.19")

    def test_http_url(self):
        text = "analyze pr: http://github.com/openshift/ovn-kubernetes/pull/3169, compare with 5.0"
        result = _parse_pr_request(text)
        assert result == ("openshift", "ovn-kubernetes", ["3169"], "5.0")


class TestSanitizeGeminiOutput:
    """Tests for _sanitize_gemini_output()."""

    def test_removes_thinking_before_marker(self):
        result = "Let me think about this...\n*Performance Impact Assessment*\nActual content"
        sanitized = _sanitize_gemini_output(result)
        assert sanitized == "*Performance Impact Assessment*\nActual content"

    def test_no_marker_returns_original(self):
        result = "Some output without the marker"
        assert _sanitize_gemini_output(result) == result

    def test_marker_at_start_returns_unchanged(self):
        result = "*Performance Impact Assessment*\nContent here"
        assert _sanitize_gemini_output(result) == result
