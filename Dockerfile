FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --break-system-packages -r /app/requirements.txt

RUN npm install -g opencode-ai@latest @openai/codex@latest @anthropic-ai/claude-code@latest

COPY router /app/router
COPY opencode.jsonc /app/opencode.jsonc

WORKDIR /workspace

CMD ["python3", "-m", "uvicorn", "router.app:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8000"]
