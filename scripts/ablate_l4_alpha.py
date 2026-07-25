#!/usr/bin/env python3
"""Replay one recorded L4 request while varying only ``alpha``.

The input must be a router ``dynamic_l4.request.json`` artifact.  The script
copies its recorded ``request`` body, replaces only the ``alpha`` field, and
calls the same call_versions endpoint for every requested value.  It records
both transport outcomes and the structural validation used by Dynamic L4.

Credentials are read from ``DYNAMIC_L4_*`` / ``CALL_VERSIONS_*`` environment
variables or explicit command-line arguments. Credentials are never embedded
in this repository or printed by this script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_ALPHAS = (0.0, 0.25, 0.5, 0.75, 1.0)
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", type=Path, required=True, help="Recorded dynamic_l4.request.json artifact.")
    parser.add_argument("--alphas", default=",".join(str(value) for value in DEFAULT_ALPHAS))
    parser.add_argument("--base-url", default=os.environ.get("DYNAMIC_L4_BASE_URL") or os.environ.get("CALL_VERSIONS_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("DYNAMIC_L4_API_KEY") or os.environ.get("CALL_VERSIONS_API_KEY", ""))
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--attempts", type=int, default=2, help="Transport attempts per alpha; validation failures are not retried.")
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0)
    parser.add_argument("--output", type=Path, help="Optional JSON report containing full model outputs.")
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be >= 1")
    return args


def load_recorded_request(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
        raise ValueError(f"{path} is not a dynamic_l4.request.json artifact")
    return dict(payload["request"])


def parse_alphas(value: str) -> list[float]:
    try:
        alphas = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("--alphas must be a comma-separated list of floats") from exc
    if not alphas:
        raise ValueError("--alphas must not be empty")
    return alphas


def endpoint_from_base_url(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/generate"):
        return cleaned
    return f"{cleaned}/generate"


def request_once(*, endpoint: str, api_key: str, body: dict[str, Any], timeout_seconds: float) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=encoded,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("call_versions response is not a JSON object")
    return payload, time.monotonic() - started


def text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(text_from_value(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                return text_from_value(value[key])
    return ""


def parse_l4_output(raw_text: str) -> tuple[str, str]:
    raw = raw_text.strip()
    if not raw:
        return "", ""
    if "</think>" in raw:
        raw = raw.rsplit("</think>", 1)[1].strip() or raw
    candidates = [raw]
    candidates.extend(re.findall(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE))
    if '\\"assistant_text\\"' in raw:
        candidates.append(raw.replace('\\"', '"').replace("\\{", "{").replace("\\}", "}"))
    decoder = json.JSONDecoder()
    decoded: list[dict[str, Any]] = []
    for candidate in candidates:
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                item, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                decoded.append(item)
    for item in reversed(decoded):
        text = text_from_value(item.get("assistant_text") or item.get("text") or item.get("response_text") or "").strip()
        tool = str(item.get("tool_name") or item.get("name") or "")
        if text:
            return text, tool
    tool_match = re.search(r"tool_name\s*[:=]\s*([A-Za-z0-9_.-]+)", raw, flags=re.IGNORECASE)
    return raw, tool_match.group(1) if tool_match else ""


def infer_validation_context(task: str) -> tuple[list[str], str]:
    available_match = re.search(r"^Available tool names:\s*(.*?)\s*$", task, flags=re.MULTILINE)
    default_match = re.search(r"^Default tool name:\s*(.*?)\s*$", task, flags=re.MULTILINE)
    available = [part.strip() for part in available_match.group(1).split(",") if part.strip()] if available_match else []
    default = default_match.group(1).strip() if default_match else ""
    return available, default


def validate_output(text: str, tool_name: str, available_tools: list[str], default_tool: str) -> tuple[bool, str]:
    if not text:
        return False, "empty assistant_text"
    if len(text) > 800:
        return False, "assistant_text too long"
    if len(re.sub(r"[\W_]+", "", text, flags=re.UNICODE)) < 6:
        return False, "assistant_text is not substantive"
    if text.lower().startswith(("assistant:", "final:", "output:", "rewrite:")):
        return False, "assistant_text includes wrapper label"
    if available_tools and tool_name not in available_tools:
        return False, "tool_name is not available"
    if not available_tools and tool_name != default_tool:
        return False, "tool_name changed without available tool list"
    return True, "assistant_text and tool_name accepted"


def run_alpha(*, alpha: float, base_request: dict[str, Any], endpoint: str, api_key: str, timeout_seconds: float, attempts: int, retry_delay_seconds: float, available_tools: list[str], default_tool: str) -> dict[str, Any]:
    body = dict(base_request)
    body["alpha"] = alpha
    errors: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        try:
            response, elapsed = request_once(endpoint=endpoint, api_key=api_key, body=body, timeout_seconds=timeout_seconds)
            raw_text = text_from_value(response.get("text"))
            assistant_text, tool_name = parse_l4_output(raw_text)
            valid, validation_reason = validate_output(assistant_text, tool_name, available_tools, default_tool)
            return {
                "alpha": alpha,
                "status": "optimized" if valid else "validation_fallback",
                "attempts": attempt,
                "elapsed_seconds": round(elapsed, 3),
                "stats": response.get("stats") if isinstance(response.get("stats"), dict) else None,
                "raw_response_characters": len(raw_text),
                "assistant_text": assistant_text,
                "assistant_text_characters": len(assistant_text),
                "tool_name": tool_name,
                "validation_reason": validation_reason,
                "raw_text": raw_text,
            }
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            errors.append({"attempt": attempt, "error_type": type(exc).__name__, "http_status": code, "message": str(exc)})
            if attempt < attempts:
                time.sleep(max(0.0, retry_delay_seconds))
    return {"alpha": alpha, "status": "transport_error", "attempts": attempts, "errors": errors}


def main() -> int:
    args = parse_args()
    base_request = load_recorded_request(args.request_file.resolve())
    if not args.base_url or not args.api_key:
        raise SystemExit("set --base-url/--api-key or DYNAMIC_L4_*/CALL_VERSIONS_* environment variables")

    endpoint = endpoint_from_base_url(args.base_url)
    alphas = parse_alphas(args.alphas)
    available_tools, default_tool = infer_validation_context(str(base_request.get("task") or ""))
    results = [
        run_alpha(
            alpha=alpha,
            base_request=base_request,
            endpoint=endpoint,
            api_key=args.api_key,
            timeout_seconds=args.timeout_seconds,
            attempts=args.attempts,
            retry_delay_seconds=args.retry_delay_seconds,
            available_tools=available_tools,
            default_tool=default_tool,
        )
        for alpha in alphas
    ]
    report = {
        "source_request": str(args.request_file.resolve()),
        "fixed_parameters": {key: value for key, value in base_request.items() if key not in {"alpha", "task", "answer", "injection", "revise"}},
        "available_tools": available_tools,
        "default_tool": default_tool,
        "results": results,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summaries = [
        {
            key: result.get(key)
            for key in ("alpha", "status", "attempts", "elapsed_seconds", "stats", "assistant_text_characters", "tool_name", "validation_reason", "errors")
        }
        for result in results
    ]
    print(json.dumps({"output": str(args.output) if args.output else None, "results": summaries}, ensure_ascii=False, indent=2))
    return 0 if all(result["status"] != "transport_error" for result in results) else 2


if __name__ == "__main__":
    sys.exit(main())
