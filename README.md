# **BugZooka**
BugZooka is a tool for log analysis and categorization based on static rules and 3rd party LLM integrations.
Product specific LLM prompts are configured in [prompts.json](prompts.json), for generic and error summarization prompts see [prompts.py](bugzooka/analysis/prompts.py). Chat interactions and sessions are not retained.
**Gen AI Notice:** users of this tool should not enter any personal information as LLM prompt input and always review generated responses for accuracy and relevance prior to using the information.

#### High-Level Flow Diagram
![Flow Diagram](assets/flow_diagram.jpg)


## **Environment Setup**

### **Prerequisites**
- Python 3.11 or higher
- pip (Python package manager)

### **Installation**

```bash
git clone <repository_url>
cd BugZooka
python -m venv venv
source venv/bin/activate

# Install dependencies
make install
```

### **Development Setup**
After cloning and setting up the virtual environment:

```bash
# Install dependencies + development tools
make dev-install

# Set up pre-commit hooks (optional)
pre-commit install

# Run tests
make test

# Run linting and formatting
make lint
make format
```

## **Usage**

### **Slack Log Analyzer Bot**
```bash
# Run via Makefile
make run ARGS="--help"

usage: entrypoint.py [-h] [--product PRODUCT] [--ci CI] [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [--enable-inference]

BugZooka - Slack Log Analyzer Bot

options:
  -h, --help            show this help message and exit
  --product PRODUCT     Product type (e.g., openshift, ansible)
  --ci CI               CI system name
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Logging level (e.g., DEBUG, INFO, WARNING, ERROR, CRITICAL). Can also be set via LOG_LEVEL env var
  --enable-inference    Enable inference mode. Can also be set via ENABLE_INFERENCE env var (true/false).
```

## **Configurables**
This tool monitors a slack channel and uses AI to provide replies to CI failure messages. Also it operates as a singleton instance.

### **Secrets**
All secrets are passed using a `.env` file which is located in the root directory of this repo. For example
```
### Mandatory fields
SLACK_BOT_TOKEN="YOUR_SLACK_BOT_TOKEN"
SLACK_CHANNEL_ID="YOUR_SLACK_CHANNEL_ID"

### Product based inference details that contain endpoint, token and model details.

// Openshift inference
OPENSHIFT_INFERENCE_URL="YOUR_INFERENCE_ENDPOINT"
OPENSHIFT_INFERENCE_TOKEN="YOUR_INFERENCE_TOKEN"
OPENSHIFT_MODEL="YOUR_INFERENCE_MODEL"

// Ansible inference
ANSIBLE_INFERENCE_URL="YOUR_INFERENCE_ENDPOINT"
ANSIBLE_INFERENCE_TOKEN="YOUR_INFERENCE_TOKEN"
ANSIBLE_MODEL="YOUR_INFERENCE_MODEL"

// Generic inference for fallback
GENERIC_INFERENCE_URL="YOUR_INFERENCE_ENDPOINT"
GENERIC_INFERENCE_TOKEN="YOUR_INFERENCE_TOKEN"
GENERIC_MODEL="YOUR_INFERENCE_MODEL"
```
**Note**: Please make sure to provide details for all the mandatory attributes and for the product that is intended to be used for testing along with fallback (i.e. GENERIC details) to handle failover use-cases.


### **Prompts**
Along with secrets, prompts are configurable using a `prompts.json` in the root directory. If not specified generic prompt will be used. Example `prompts.json` content
```
{
  "OPENSHIFT_PROMPT": {
    "system": "You are an expert in OpenShift, Kubernetes, and cloud infrastructure. Your task is to analyze logs and summaries related to OpenShift environments. Given a log summary, identify the root cause, potential fixes, and affected components. Be precise and avoid generic troubleshooting steps. Prioritize OpenShift-specific debugging techniques.",
    "user": "Here is the log summary from an OpenShift environment:\n\n{summary}\n\nBased on this summary, provide a structured breakdown of:\n- The OpenShift component likely affected (e.g., etcd, kube-apiserver, ingress, SDN, Machine API)\n- The probable root cause\n- Steps to verify the issue further\n- Suggested resolution, including OpenShift-specific commands or configurations.",
    "assistant": "**Affected Component:** <Identified component>\n\n**Probable Root Cause:** <Describe why this issue might be occurring>\n\n**Verification Steps:**\n- <Step 1>\n- <Step 2>\n- <Step 3>\n\n**Suggested Resolution:**\n- <OpenShift CLI commands>\n- <Relevant OpenShift configurations>"
  },
  "ANSIBLE_PROMPT": {
    "system": "You are an expert in Ansible automation, playbook debugging, and infrastructure as code (IaC). Your task is to analyze log summaries related to Ansible execution, playbook failures, and task errors. Given a log summary, identify the root cause, affected tasks, and potential fixes. Prioritize Ansible-specific debugging techniques over generic troubleshooting.",
    "user": "Here is the log summary from an Ansible execution:\n\n{summary}\n\nBased on this summary, provide a structured breakdown of:\n- The failed Ansible task and module involved\n- The probable root cause\n- Steps to reproduce or verify the issue\n- Suggested resolution, including relevant playbook changes or command-line fixes.",
    "assistant": "**Failed Task & Module:** <Identified task and module>\n\n**Probable Root Cause:** <Describe why the failure occurred>\n\n**Verification Steps:**\n- <Step 1>\n- <Step 2>\n- <Step 3>\n\n**Suggested Resolution:**\n- <Ansible CLI commands>\n- <Playbook modifications or configuration changes>"
  }
}
```

### **Containerized Deployment**
```bash
# Build image using Podman
podman build -f Dockerfile -t quay.io/YOUR_REPO/bugzooka:latest .

# Push to registry
podman push quay.io/YOUR_REPO/bugzooka:latest

# Run as a container
podman run -d \
  -e PRODUCT=openshift \
  -e CI=prow \
  -e ENABLE_INFERENCE=true \
  -v /path-to/prompts.json:/app/prompts.json:Z \
  -v /path-to/.env:/app/.env:Z \
  quay.io/YOUR_REPO/bugzooka:latest

# Alternatively use Make commands
make podman-build
make podman-run  # Requires .env file in project root
```

## **Development**

### **Project Structure**
```
bugzooka/
├── __init__.py
├── entrypoint.py              # Main orchestrator
├── core/                      # Core application functionality
│   ├── __init__.py
│   ├── config.py             # Configuration management
│   ├── constants.py          # Application constants
│   └── utils.py              # Shared utility functions
├── integrations/              # External service integrations
│   ├── __init__.py
│   ├── slack_fetcher.py      # Slack integration
│   ├── gemini_client.py      # Gemini API client
│   └── inference.py          # Generic inference API
└── analysis/                  # Log analysis and processing
    ├── __init__.py
    ├── log_analyzer.py       # Main log analysis orchestration
    ├── log_summarizer.py     # Log summarization functionality
    ├── prow_analyzer.py      # Prow-specific CI/CD analysis
    ├── xmlparser.py          # XML parsing for test results
    └── prompts.py            # AI prompts and templates
```

### **Code Quality**
This project uses the following tooling for code quality:

- **Black**: Code formatting
- **Ruff**: Fast Python linter (replaces flake8, isort, etc.)
- **MyPy**: Static type checking
- **Pre-commit**: Git hooks for code quality (optional)
- **Pytest**: Testing framework
