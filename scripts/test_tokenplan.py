#!/usr/bin/env python3
"""Smoke-test the TokenPlan OpenAI-compatible API without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TokenPlanConfig:
    base_url: str
    api_key: str
    model: str


def env_file_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_config(path: Path) -> TokenPlanConfig:
    values = env_file_values(path)

    def value(name: str) -> str:
        return os.environ.get(name, values.get(name, "")).strip()

    return TokenPlanConfig(
        base_url=value("DEEPSEEK_BASE_URL").rstrip("/"),
        api_key=value("DEEPSEEK_API_KEY"),
        model=value("DEEPSEEK_MODEL"),
    )


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def summarize_error(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return json.dumps(payload, ensure_ascii=False)[:300]
    code = str(error.get("code") or "")
    message = str(error.get("message") or "")
    message = re.sub(r"(?i)(api[ _-]?key)\s+\S+", r"\1 [redacted]", message)
    parts = []
    if code:
        parts.append(f"code={code}")
    if message:
        parts.append(f"message={message}")
    return "; ".join(parts) or "unknown API error"


def main() -> int:
    parser = argparse.ArgumentParser(description="Test the configured TokenPlan API key.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    config = load_config(args.env_file)
    if not config.base_url or not config.api_key or not config.model:
        print("Missing DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY, or DEEPSEEK_MODEL.", file=sys.stderr)
        return 2

    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": "Reply with exactly: tokenplan-ok"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        status, body = post_json(
            f"{config.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "tokenplan-smoke-test/1.0",
            },
            payload,
            args.timeout,
        )
    except urllib.error.URLError as exc:
        print(f"TokenPlan request failed: {exc.reason}", file=sys.stderr)
        return 1

    if not 200 <= status < 300:
        print(f"TokenPlan request failed: HTTP {status}; {summarize_error(body)}", file=sys.stderr)
        return 1

    response = json.loads(body)
    choices = response.get("choices") if isinstance(response, dict) else []
    content = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = str(message.get("content") or "")
    print(json.dumps({"status": status, "model": response.get("model", config.model), "content": content}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
