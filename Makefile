.PHONY: help install dev-install test lint format check clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package dependencies
	pip install -r requirements.txt

dev-install:  ## Install the package with development dependencies
	pip install -r requirements.txt
	pip install pytest pytest-cov pytest-asyncio black ruff mypy types-requests types-PyYAML pre-commit

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

run:  ## Run BugZooka (requires --product and --ci arguments)
	PYTHONPATH=. python bugzooka/entrypoint.py $(ARGS)

podman-build:  ## Build podman image
	podman build -t bugzooka:latest .

podman-run:  ## Run podman container
	podman run -d \
		-e PRODUCT=openshift \
		-e CI=prow \
		-e ENABLE_INFERENCE=true \
		-e ANALYSIS_MODE=gemini \
		-e GEMINI_VERIFY_SSL=false \
		-v ./.env:/app/.env:Z \
		bugzooka:latest
