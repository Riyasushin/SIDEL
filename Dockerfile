FROM node:22-bookworm-slim

ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        "deb ${DEBIAN_MIRROR} bookworm main contrib non-free non-free-firmware" \
        "deb ${DEBIAN_MIRROR} bookworm-updates main contrib non-free non-free-firmware" \
        "deb ${DEBIAN_SECURITY_MIRROR} bookworm-security main contrib non-free non-free-firmware" \
        > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install \
        --break-system-packages \
        --index-url "${PIP_INDEX_URL}" \
        -r /app/requirements.txt

RUN npm config set registry "${NPM_REGISTRY}" \
    && npm install -g @openai/codex@latest @anthropic-ai/claude-code@latest

COPY code_agents.py /app/code_agents.py
COPY router /app/router
COPY data /app/data

WORKDIR /workspace

CMD ["python3", "-m", "uvicorn", "router.app:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8000"]
