.PHONY: help install dev-install test lint format check clean deploy

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package dependencies
	pip install -r requirements.txt

dev-install:  ## Install the package with development dependencies
	pip install -r requirements-dev.txt

test:  ## Run tests
	pytest tests/ -v

lint:  ## Run linting
	ruff check bugzooka/ tests/
	mypy bugzooka/

format:  ## Format code
	black bugzooka/ tests/
	ruff format bugzooka/ tests/

check: lint test  ## Run all checks

clean:  ## Clean cache and temporary files
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache/
	rm -rf .coverage
	rm -rf .ruff_cache/
	rm -rf .mypy_cache/

run:  ## Run BugZooka (if RAG_IMAGE set in env/.env, apply sidecar overlay first)
	PYTHONPATH=. python bugzooka/entrypoint.py $(ARGS)

podman-build:  ## Build podman image
	podman build -t bugzooka:latest .

podman-run:  ## Run podman container
	podman run -d \
		-e ENABLE_INFERENCE=true \
		-v ./.env:/app/.env:Z \
		bugzooka:latest

deploy:  ## Deploy to OpenShift (uses overlays/rag if RAG_IMAGE provided, overlays/chatbot if CHATBOT is true)
	@set -a; \
	if [ -f .env ]; then . ./.env; fi; \
	set +a; \
	if [ -n "$$RAG_IMAGE" ]; then \
		echo "Deploying with RAG overlay (RAG_IMAGE=$$RAG_IMAGE)"; \
		kustomize build --load-restrictor=LoadRestrictionsNone ./kustomize/overlays/rag | envsubst | oc apply -f -; \
	elif [ "$$CHATBOT" = "true" ]; then \
		echo "Deploying with chatbot overlay"; \
		kustomize build --load-restrictor=LoadRestrictionsNone ./kustomize/overlays/chatbot | envsubst | oc apply -f -; \
	else \
		echo "Deploying base default overlay"; \
		kustomize build --load-restrictor=LoadRestrictionsNone ./kustomize/base | envsubst | oc apply -f -; \
	fi

undeploy:
	@set -a; \
	if [ -f .env ]; then . ./.env; fi; \
	set +a; \
	if [ -n "$$RAG_IMAGE" ]; then \
		echo "Deploying with RAG overlay (RAG_IMAGE=$$RAG_IMAGE)"; \
		kustomize build --load-restrictor=LoadRestrictionsNone ./kustomize/overlays/rag | envsubst | oc delete -f -; \
	elif [ "$$CHATBOT" = "true" ]; then \
		echo "Deploying with chatbot overlay"; \
		kustomize build --load-restrictor=LoadRestrictionsNone ./kustomize/overlays/chatbot | envsubst | oc delete -f -; \
	else \
		echo "Deploying base default overlay"; \
		kustomize build --load-restrictor=LoadRestrictionsNone ./kustomize/base | envsubst | oc delete -f -; \
	fi