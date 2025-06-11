import os
import re
import logging
import signal
import sys
import time
import argparse
from langchain.tools import Tool
from langchain.chat_models import ChatOpenAI
from langchain.agents import initialize_agent
from langchain.agents import AgentType
from src.config import SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, get_product_config
from src.prompts import ERROR_FILTER_PROMPT
from src.log_summarizer import download_prow_logs, generate_prompt, download_url_to_log
from src.inference import ask_inference_api, analyze_log
from src.handle_error_types import process_build_log
from slack_sdk import WebClient
from src.utils import extract_link
from slack_sdk.errors import SlackApiError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("slack_fetcher.log"),  # Log to a file
        logging.StreamHandler(sys.stdout)  # Log to console
    ]
)

class SlackMessageFetcher:
    """Continuously fetches new messages from a Slack channel and logs them."""

    def __init__(self, channel_id, poll_interval=10):
        """Initialize Slack client and channel details."""
        self.SLACK_BOT_TOKEN = SLACK_BOT_TOKEN
        self.CHANNEL_ID = channel_id
        self.POLL_INTERVAL = poll_interval  # How often to fetch messages
        self.last_seen_timestamp = None  # Track the latest message timestamp

        if not self.SLACK_BOT_TOKEN:
            logging.error("Missing SLACK_BOT_TOKEN environment variable.")
            sys.exit(1)

        self.client = WebClient(token=self.SLACK_BOT_TOKEN)
        self.running = True  # Control flag for loop

        # Handle SIGINT (Ctrl+C) for graceful exit
        signal.signal(signal.SIGINT, self.shutdown)

    def fetch_messages(self, **kwargs):
        """Fetches only the latest messages from the Slack channel."""
        try:
            product = kwargs["product"]
            ci_system = kwargs["ci"]
            mapping_config = kwargs["mapping_config"]
            product_config = kwargs["product_config"]
            params = {"channel": self.CHANNEL_ID, "limit": 1}
            if self.last_seen_timestamp:
                params["oldest"] = self.last_seen_timestamp  # Fetch only new messages

            response = self.client.conversations_history(**params)
            messages = response.get("messages", [])

            if messages:
                new_message = None
                for msg in reversed(messages):  # Oldest first
                    ts = msg.get("ts")  # Message timestamp
                    if self.last_seen_timestamp is None or float(ts) > float(self.last_seen_timestamp):
                        new_message = msg
                        break

                if new_message:
                    user = msg.get("user", "Unknown")
                    text = msg.get("text", "No text available")
                    ts = msg.get("ts")
                    logging.info(f"📩 New message from {user}: {text}")
                    self.last_seen_timestamp = ts  # Update latest timestamp
                    
                    if ci_system == "PROW":
                        job_url = extract_link(text)
                        directory_path = download_prow_logs(job_url)
                    else:
                        # Pre-assumes the other ci system is ansible
                        url_pattern = r"<([^>]+)>"
                        match = re.search(url_pattern, text)
                        if match:
                            url = match.group(1)
                            logging.info(f"Ansible job url: {url}")
                            directory_path = download_url_to_log(url, "/build-log.txt")

                    
                    for error_context in process_build_log("/tmp/build-log.txt"):
                        full_errors_list, step_type, step_name, step_line_num = error_context

                        self.client.chat_postMessage(
                            channel=self.CHANNEL_ID,
                            text=(
                            f":red_circle: *This failure is in a {step_type} step. Step {step_name} failed at line number {step_line_num}.*\n"
                            ),
                            thread_ts=ts
                        )

                        full_errors_list = list(set(full_errors_list))
                        if len(full_errors_list) > 10:
                            full_errors_list = full_errors_list[:10]
                        error_prompt = ERROR_FILTER_PROMPT["user"].format(error_list="\n".join(full_errors_list))



                        response = ask_inference_api(
                            messages = [
                                {"role": "system", "content": ERROR_FILTER_PROMPT["system"]},
                                {"role": "user", "content": error_prompt},
                                {"role": "assistant", "content": ERROR_FILTER_PROMPT["assistant"]}
                            ],
                            url=product_config["endpoint"]["GENERIC"],
                            api_token=product_config["token"]["GENERIC"],
                            model=product_config["model"]["GENERIC"]
                        )

                        # Convert JSON response to a Python list
                        errors_list = response.split("\n")
                        errors_list_string = "\n".join(errors_list)

                        self.client.chat_postMessage(
                            channel=self.CHANNEL_ID,
                            text=(
                                ":mag_right: *Error Logs Preview*"
                                "\n```"
                                f"\n{errors_list_string}\n"
                                "```"
                            ),
                            thread_ts=ts
                        )
                        error_prompt = generate_prompt(errors_list)
                        error_summary = ask_inference_api(messages=error_prompt, url=product_config["endpoint"]["GENERIC"], api_token=product_config["token"]["GENERIC"], model=product_config["model"]["GENERIC"])
                        llm = ChatOpenAI(model_name=product_config["model"]["GENERIC"], openai_api_key=product_config["token"]["GENERIC"], base_url=product_config["endpoint"]["GENERIC"]+"/v1")
                        product_tool = Tool(
                            name="Product Log Analyzer",
                            func=analyze_log(product, product_config),
                            description="Use this tool for product related log summaries. Provide input as JSON with 'log_summary', 'product', and 'product_config'."
                        )
                        generic_tool = Tool(
                            name="Generic Log Analyzer",
                            func=analyze_log("GENERIC", product_config),
                            description="Use this tool for any general log summaries. Provide input as JSON with 'log_summary', 'product', and 'product_config'."
                        )
                        TOOLS = [product_tool, generic_tool]
                        agent = initialize_agent(
                            tools=TOOLS,
                            llm=llm,
                            agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
                            verbose=True
                        )
                        if product: 
                            response = agent.run(f"This log is classified as product. Please analyze the summary: {error_summary}")
                        else:
                            response = agent.run(f"This log is classified as generic. Please analyze the summary: {error_summary}")
                        self.client.chat_postMessage(
                            channel=self.CHANNEL_ID,
                            text=(
                                ":fast_forward: *Implications to understand*"
                                "\n```"
                                f"\n{response}\n"
                                "```"
                            ),
                            thread_ts=ts
                        )
                else:
                    logging.info("⏳ No new messages.")

        except SlackApiError as e:
            logging.error(f"❌ Slack API Error: {e.response['error']}")
        except Exception as e:
            logging.error(f"⚠️ Unexpected Error: {str(e)}")

    def run(self, **kwargs):
        """
        Continuously fetch only new messages every X seconds until interrupted.

        :param kwargs: arguments to run the application.
        """
        logging.info(f"🚀 Starting Slack Message Fetcher for Channel: {self.CHANNEL_ID}")
        try:
            while self.running:
                self.fetch_messages(**kwargs)
                time.sleep(self.POLL_INTERVAL)  # Wait before next fetch
        except Exception as e:
            logging.error(f"Unexpected failure: {str(e)}")
        finally:
            logging.info("👋 Shutting down gracefully.")

    def shutdown(self, signum, frame):
        """Handles graceful shutdown on user interruption."""
        logging.info("🛑 Received exit signal. Stopping message fetcher...")
        self.running = False
        sys.exit(0)

# export PYTHONPATH=$(pwd)/src:$PYTHONPATH
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Slack Log Analyzer Bot")

    parser.add_argument("--product", type=str, default=os.environ.get("PRODUCT"), help="Product type (e.g., openshift, ansible)")
    parser.add_argument("--ci", type=str, default=os.environ.get("CI"), help="CI system name")
    parser.add_argument("--mapping-config", type=str, default=os.environ.get("MAPPING_CONFIG"), help="Path to mapping config")

    args = parser.parse_args()
    missing_args = []
    if not args.product:
        missing_args.append("product or PRODUCT")
    if not args.ci:
        missing_args.append("ci or CI")
    if missing_args:
        logging.error(f"Missing required arguments or env vars: {', '.join(missing_args)}")
        sys.exit(1)
    
    kwargs = {
        "product": args.product.upper(),
        "ci": args.ci.upper(),
        "mapping_config": args.mapping_config,
        "product_config": get_product_config(product_name=args.product.upper()),
    }

    fetcher = SlackMessageFetcher(channel_id=SLACK_CHANNEL_ID, poll_interval=10)
    fetcher.run(**kwargs)

