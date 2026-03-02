# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Runtime stage ----
FROM python:3.12-slim

# Install FFmpeg (required for media merging)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY server.py .
COPY .env.example .env

# Cloud Run provides PORT env var (default 8080)
ENV PORT=8080

# Run in SSE mode on the port Cloud Run assigns
CMD ["sh", "-c", "python server.py --transport sse --port $PORT"]
