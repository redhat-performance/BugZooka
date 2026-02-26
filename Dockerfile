# ================================
# Stage 1: Build stage
# ================================
FROM python:3.11-slim as builder

# Install build dependencies in a single layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    bzip2 \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

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

# Install Python dependencies (including gsutil) to a target directory
RUN pip install --no-cache-dir --target /app-packages -r requirements.txt gsutil

# ================================
# Stage 2: Runtime stage
# ================================
FROM python:3.11-slim as runtime

# Install only runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    ca-certificates \
    vim \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder stage
COPY --from=builder /app-packages /app-packages

# Copy logjuicer binary from builder stage
COPY --from=builder /usr/local/bin/logjuicer /usr/local/bin/logjuicer

# Create /app directory and change ownership
RUN mkdir -p /app /data /logs /tmp /.config /.gsutil \
    && chgrp -R 0 /app /data /logs /tmp /.config /.gsutil  \
    && chmod -R g+rwX /app /data /logs /tmp /.config /.gsutil

# Set working directory
WORKDIR /app

# Copy source code (do this last for better layer caching)
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH="/app-packages:$PYTHONPATH"
ENV PATH="/app-packages/bin:$PATH"

# Switch to non-root user
USER 1001

# Default command
CMD ["python3", "bugzooka/entrypoint.py"]
