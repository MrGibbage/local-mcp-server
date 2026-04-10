FROM python:3.12-slim

# Install SSH client tools (used by paramiko for host key scanning if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
RUN useradd --create-home --shell /bin/bash mcp

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .

# Keys and config are mounted at runtime — not baked into the image
# ./keys/  → /keys/
# ./config.yaml → /app/config.yaml

USER mcp

ENV CONFIG_PATH=/app/config.yaml

EXPOSE 8080

CMD ["python", "server.py"]
