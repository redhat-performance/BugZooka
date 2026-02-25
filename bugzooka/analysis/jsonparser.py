import logging

logger = logging.getLogger(__name__)

SEPARATOR = "\u2500" * 60


def extract_json_changepoints(json_data, max_prs=None):
    """
    Extract changepoint summaries from JSON data.

    Each changepoint entry produces a multi-line block with version info,
    regressed metrics, and the PRs introduced between nightlies.

    :param json_data: List of changepoint records
    :param max_prs: Maximum PRs to display per changepoint (None = all)
    :return: list of formatted changepoint summary strings (one per entry)
    """
    cp_entries = [e for e in json_data if e.get("is_changepoint", False)]
    total = len(cp_entries)

    changepoints = []
    for idx, entry in enumerate(cp_entries, 1):
        github_ctx = entry.get("github_context", {})
        current_version = github_ctx.get(
            "current_version", entry.get("ocpVersion", "unknown")
        )
        previous_version = github_ctx.get("previous_version", "unknown")
        prs = entry.get("prs", [])
        metrics = entry.get("metrics", {})

        regressed = []
        for metric_name, metric_data in metrics.items():
            percentage = metric_data.get("percentage_change", 0)
            if percentage != 0:
                sign = "+" if percentage > 0 else ""
                regressed.append(f"{metric_name}: {sign}{percentage:.2f}%")

        if not regressed:
            continue

        regressed_summary = ", ".join(regressed)
        lines = [
            f"{SEPARATOR}",
            f"  Changepoint {idx} of {total}: {regressed_summary}",
            f"{SEPARATOR}",
            f"Version: {current_version}",
            f"Previous: {previous_version}",
        ]

        if prs:
            display_prs = prs[:max_prs] if max_prs is not None else prs
            lines.append(f"\nPRs between nightlies ({len(prs)}):")
            for pr in display_prs:
                lines.append(f"  {pr}")
            if max_prs is not None and len(prs) > max_prs:
                lines.append(f"  ... and {len(prs) - max_prs} more")

        changepoints.append("\n".join(lines))

    return changepoints
