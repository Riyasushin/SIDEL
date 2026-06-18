#!/usr/bin/env python3
"""Minimal Kimi Code Plan authentication smoke test.

Reads KIMI_API_KEY from the environment or .env, calls the Kimi Code
Anthropic-compatible endpoint by default, and never prints the secret value.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "kimi-for-coding"
DEFAULT_OPENAI_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.kimi.com/coding"


def load_env_key(path: Path) -> str:
    if os.environ.get("KIMI_API_KEY"):
        return os.environ["KIMI_API_KEY"].strip()
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith("KIMI_API_KEY="):
            continue
        return stripped.split("=", 1)[1].strip().strip('"\'')
    return ""


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
    if isinstance(error, dict):
        pieces = []
        if error.get("type"):
            pieces.append(f"type={error['type']}")
        if error.get("message"):
            pieces.append(f"message={error['message']}")
        return "; ".join(pieces) or json.dumps(error, ensure_ascii=False)[:300]
    return json.dumps(payload, ensure_ascii=False)[:300]


def check_openai(args: argparse.Namespace, key: str) -> bool:
    url = args.openai_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 8,
        "temperature": 0,
    }
    status, body = post_json(
        url,
        {
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
            "user-agent": "swebench-kimi-code-auth-smoke/1.0",
        },
        payload,
        args.timeout,
    )
    if 200 <= status < 300:
        print(f"openai-compatible auth ok: {url} model={args.model}")
        return True
    print(f"openai-compatible auth failed: status={status} {summarize_error(body)}", file=sys.stderr)
    return False


def check_anthropic(args: argparse.Namespace, key: str) -> bool:
    url = args.anthropic_base_url.rstrip("/") + "/v1/messages"
    payload = {
        "model": args.model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply with OK."}],
    }
    status, body = post_json(
        url,
        {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "user-agent": "swebench-kimi-code-auth-smoke/1.0",
        },
        payload,
        args.timeout,
    )
    if 200 <= status < 300:
        print(f"anthropic-compatible auth ok: {url} model={args.model}")
        return True
    print(f"anthropic-compatible auth failed: status={status} {summarize_error(body)}", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--openai-base-url", default=DEFAULT_OPENAI_BASE_URL)
    parser.add_argument("--anthropic-base-url", default=DEFAULT_ANTHROPIC_BASE_URL)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--openai", action="store_true", help="Also test the OpenAI-compatible endpoint, which may require Kimi client allowlisting")
    args = parser.parse_args()

    key = load_env_key(args.env_file)
    if not key:
        print("KIMI_API_KEY is missing from the environment and --env-file", file=sys.stderr)
        return 2

    ok = check_anthropic(args, key)
    if args.openai:
        ok = check_openai(args, key) and ok
    if not ok:
        print("Kimi Code auth failed. Verify .env contains a complete Kimi Code Plan key.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
