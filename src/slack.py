import os
import logging
import signal
import sys
import time
from langchain.tools import Tool
from langchain.chat_models import ChatOpenAI
from langchain.agents import initialize_agent
from langchain.agents import AgentType
from src.config import SLACK_BOT_TOKEN
from src.config import INFERENCE_ENDPOINTS, INFERENCE_TOKENS, MODEL_MAP
from src.log_summarizer import download_prow_logs, search_errors_in_file, generate_prompt
from src.inference import ask_inference_api, analyze_openshift_log, analyze_ansible_log, analyze_generic_log
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
    """Continuously fetches new messages from multiple Slack channels and logs them."""

    def __init__(self, channel_ids, poll_interval=10):
        """Initialize Slack client and channel details.
        
        Args:
            channel_ids (list): List of Slack channel IDs to monitor
            poll_interval (int): How often to fetch messages (in seconds)
        """
        self.SLACK_BOT_TOKEN = SLACK_BOT_TOKEN
        self.CHANNEL_IDS = channel_ids if isinstance(channel_ids, list) else [channel_ids]  # Support backward compatibility
        self.POLL_INTERVAL = poll_interval  # How often to fetch messages
        self.last_seen_timestamps = {channel: None for channel in self.CHANNEL_IDS}  # Track timestamps per channel

        if not self.SLACK_BOT_TOKEN:
            logging.error("Missing SLACK_BOT_TOKEN environment variable.")
            sys.exit(1)

        self.client = WebClient(token=self.SLACK_BOT_TOKEN)
        self.running = True  # Control flag for loop

        # Handle SIGINT (Ctrl+C) for graceful exit
        signal.signal(signal.SIGINT, self.shutdown)

    def fetch_messages(self):
        """Fetches only the latest messages from all monitored Slack channels."""
        for channel_id in self.CHANNEL_IDS:
            try:
                params = {"channel": channel_id, "limit": 1}
                if self.last_seen_timestamps[channel_id]:
                    params["oldest"] = self.last_seen_timestamps[channel_id]  # Fetch only new messages

                response = self.client.conversations_history(**params)
                messages = response.get("messages", [])

                if messages:
                    new_messages = []
                    for msg in reversed(messages):  # Oldest first
                        ts = msg.get("ts")  # Message timestamp
                        if self.last_seen_timestamps[channel_id] is None or float(ts) > float(self.last_seen_timestamps[channel_id]):
                            new_messages.append(msg)

                    if new_messages:
                        for msg in new_messages:
                            user = msg.get("user", "Unknown")
                            text = msg.get("text", "No text available")
                            ts = msg.get("ts")
                            logging.info(f"📩 New message in channel {channel_id} from {user}: {text}")
                            self.last_seen_timestamps[channel_id] = ts  # Update latest timestamp
                            job_url = extract_link(text)
                            if job_url:  # Only process logs if a valid URL was found
                                directory_path = download_prow_logs(job_url)
                                errors_list = search_errors_in_file(directory_path + "/build-log.txt")
                                if len(errors_list) > 5:
                                    errors_list = errors_list[:5]
                                errors_list_string = "\n".join(errors_list)
                                self.client.chat_postMessage(
                                    channel=channel_id,
                                    text=(
                                        ":checking: *Error Logs Preview*"
                                        "\n```"
                                        f"\n{errors_list_string}\n"
                                        "```"
                                    ),
                                    thread_ts=ts
                                )
                                error_prompt = generate_prompt(errors_list)
                                error_summary = ask_inference_api(messages=error_prompt, url=INFERENCE_ENDPOINTS["Generic"], api_token=INFERENCE_TOKENS["Generic"], model=MODEL_MAP["Generic"])
                                self.client.chat_postMessage(
                                    channel=channel_id,
                                    text=(
                                        ":thought_balloon: *Initial Thoughts*"
                                        "\n```"
                                        f"\n{error_summary}\n"
                                        "```"
                                    ),
                                    thread_ts=ts
                                )
                                llm = ChatOpenAI(model_name=MODEL_MAP["Generic"], openai_api_key=INFERENCE_TOKENS["Generic"], base_url=INFERENCE_ENDPOINTS["Generic"]+"/v1")
                                openshift_tool = Tool(
                                    name="OpenShift Log Analyzer",
                                    func=analyze_openshift_log,
                                    description="Use this tool for OpenShift-related log summaries."
                                )
                                ansible_tool = Tool(
                                    name="Ansible Log Analyzer",
                                    func=analyze_ansible_log,
                                    description="Use this tool for Ansible-related log summaries."
                                )
                                generic_tool = Tool(
                                    name="Generic Log Analyzer",
                                    func=analyze_generic_log,
                                    description="Use this tool for general logs not specific to OpenShift or Ansible."
                                )
                                TOOLS = [openshift_tool, ansible_tool, generic_tool]
                                agent = initialize_agent(
                                    tools=TOOLS,
                                    llm=llm,
                                    agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
                                    verbose=True
                                )
                                response = agent.run(f"The log is classified as Openshift. Analyze the following summary: {error_summary}")
                                print(response)
                                self.client.chat_postMessage(
                                    channel=channel_id,
                                    text=(
                                        ":done_it_is: *Final Thoughts*"
                                        "\n```"
                                        f"\n{response}\n"
                                        "```"
                                    ),
                                    thread_ts=ts
                                )
                else:
                    logging.debug(f"⏳ No new messages in channel {channel_id}.")

            except SlackApiError as e:
                logging.error(f"❌ Slack API Error: {e.response['error']}")
            except Exception as e:
                logging.error(f"⚠️ Unexpected Error: {str(e)}")

    def run(self):
        """Continuously fetch only new messages every X seconds until interrupted."""
        channel_list = ", ".join(self.CHANNEL_IDS)
        logging.info(f"🚀 Starting Slack Message Fetcher for Channels: {channel_list}")
        try:
            while self.running:
                self.fetch_messages()
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
# bugzooka-ansible channel C08JPSC5HHV
if __name__ == "__main__":
    # Example of monitoring multiple channels
    channel_ids = ["C08JS8BVDJ8", "C08JPSC5HHV"]  # bugzooka-general and bugzooka-ansible channels
    fetcher = SlackMessageFetcher(channel_ids=channel_ids, poll_interval=10)
    fetcher.run()
