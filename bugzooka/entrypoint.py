import argparse
import logging
import os
import sys

from bugzooka.core.config import (
    SLACK_CHANNEL_ID,
    get_product_config,
    configure_logging,
)
from bugzooka.core.constants import (
    SLACK_POLL_INTERVAL,
)
from bugzooka.integrations.slack_fetcher import SlackMessageFetcher
from bugzooka.core.utils import str_to_bool


def main() -> None:
    """Main entrypoint for BugZooka."""
    VALID_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    parser = argparse.ArgumentParser(description="BugZooka - Slack Log Analyzer Bot")

    parser.add_argument(
        "--product",
        type=str,
        default=os.environ.get("PRODUCT"),
        help="Product type (e.g., openshift, ansible)",
    )
    parser.add_argument(
        "--ci", type=str, default=os.environ.get("CI"), help="CI system name"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=VALID_LOG_LEVELS,
        default=os.environ.get("LOG_LEVEL", "INFO").upper(),
        help="Logging level (e.g., DEBUG, INFO, WARNING, ERROR, CRITICAL)."
        " Can also be set via LOG_LEVEL env var",
    )
    parser.add_argument(
        "--enable-inference",
        action="store_true",
        default=str_to_bool(os.environ.get("ENABLE_INFERENCE", "false")),
        help="Enable inference mode. Can also be set via ENABLE_INFERENCE env var (true/false).",
    )

    args = parser.parse_args()
    configure_logging(args.log_level)
    logger = logging.getLogger(__name__)

    missing_args = []
    if not args.product:
        missing_args.append("product or PRODUCT")
    if not args.ci:
        missing_args.append("ci or CI")
    if missing_args:
        logger.error("Missing required arguments or env vars: {%s}", missing_args)
        sys.exit(1)

    kwargs = {
        "product": args.product.upper(),
        "ci": args.ci.upper(),
        "product_config": get_product_config(product_name=args.product.upper()),
        "enable_inference": args.enable_inference,
    }

    fetcher = SlackMessageFetcher(
        channel_id=SLACK_CHANNEL_ID, logger=logger, poll_interval=SLACK_POLL_INTERVAL
    )
    fetcher.run(**kwargs)


if __name__ == "__main__":
    main()
