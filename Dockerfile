# ================================
# Stage 1: Build stage
# ================================
FROM python:3.11-slim as builder

# Install build dependencies in a single layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    bzip2 \
    gnupg \
    ca-certificates \
    apt-transport-https \
    lsb-release \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Google Cloud SDK
RUN curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] http://packages.cloud.google.com/apt cloud-sdk main" \
    > /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-cloud-sdk && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Get logjuicer latest version tag dynamically and download + install binary
RUN set -eux; \
    LATEST_VERSION=$(curl -s https://api.github.com/repos/logjuicer/logjuicer/releases/latest | jq -r .tag_name); \
    DOWNLOAD_URL="https://github.com/logjuicer/logjuicer/releases/download/${LATEST_VERSION}/logjuicer-x86_64-linux.tar.bz2"; \
    curl -L "$DOWNLOAD_URL" -o /tmp/logjuicer.tar.bz2; \
    tar -xjf /tmp/logjuicer.tar.bz2 -C /usr/local/; \
    chmod +x /usr/local/bin/logjuicer; \
    rm /tmp/logjuicer.tar.bz2

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies to a target directory for easy copying
RUN pip install --no-cache-dir --target /app-packages -r requirements.txt

# ================================
# Stage 2: Runtime stage
# ================================
FROM python:3.11-slim as runtime

# Install only runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /bin/bash appuser

# Copy Python packages from builder stage
COPY --from=builder /app-packages /app-packages

# Copy Google Cloud SDK from builder stage
COPY --from=builder /usr/lib/google-cloud-sdk /usr/lib/google-cloud-sdk
COPY --from=builder /usr/bin/gcloud /usr/bin/gcloud
COPY --from=builder /usr/bin/gsutil /usr/bin/gsutil

# Copy logjuicer binary from builder stage
COPY --from=builder /usr/local/bin/logjuicer /usr/local/bin/logjuicer

# Create /app directory and change ownership
RUN mkdir -p /app && chown appuser:appuser /app

# Set working directory
WORKDIR /app

# Copy source code (do this last for better layer caching)
COPY --chown=appuser:appuser . .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH="/app-packages:$PYTHONPATH"
ENV PATH="/usr/lib/google-cloud-sdk/bin:/app-packages/bin:$PATH"

# Switch to non-root user
USER appuser

# Default command
CMD ["python3", "slack.py"]
