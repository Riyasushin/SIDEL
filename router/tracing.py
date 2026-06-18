import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{12,}")
_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "x_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "kimi_api_key",
    "cookie",
    "set_cookie",
}
_TURN_COUNTERS: dict[str, int] = defaultdict(int)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _SECRET_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_PATTERN.sub("sk-<redacted>", value)
    return value


def trace_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "router_trace_dir", ""))


def extract_trace_metadata(body: dict[str, Any], settings: Any, endpoint: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "trace_id": f"trace_{uuid.uuid4().hex}",
        "endpoint": endpoint,
        "run_id": getattr(settings, "router_trace_run_id", "") or "local",
        "agent": getattr(settings, "router_trace_agent", "") or infer_agent(endpoint, body),
        "instance_id": getattr(settings, "router_trace_instance_id", "") or "unknown",
        "request_role": infer_request_role(endpoint, body),
    }
    key = f"{metadata['run_id']}:{metadata['agent']}:{metadata['instance_id']}:{endpoint}"
    _TURN_COUNTERS[key] += 1
    metadata["turn_index"] = _TURN_COUNTERS[key]
    return metadata


def infer_agent(endpoint: str, body: dict[str, Any]) -> str:
    model = str(body.get("model") or "").lower()
    if "claude" in model or endpoint.startswith("anthropic"):
        return "claude"
    if endpoint.startswith("openai.responses"):
        return "codex"
    return "opencode"


def infer_request_role(endpoint: str, body: dict[str, Any]) -> str:
    model = str(body.get("model") or "").lower()
    if endpoint.startswith("openai.responses") and model == "codex-auto-review":
        return "codex_auto_review"
    if endpoint.startswith("anthropic") and looks_like_claude_auto_review(body):
        return "claude_auto_review"
    return "agent_main"


def looks_like_claude_auto_review(body: dict[str, Any]) -> bool:
    text = json.dumps(body, ensure_ascii=False)
    markers = (
        "<transcript>",
        "HARD BLOCK",
        "SOFT BLOCK",
        "Classification Process",
        "Output Format",
        "<block>yes</block>",
        "<block>no</block>",
    )
    return sum(marker in text for marker in markers) >= 2


def write_trace_event(settings: Any, metadata: dict[str, Any], event: str, payload: dict[str, Any]) -> None:
    trace_dir = getattr(settings, "router_trace_dir", "")
    if not trace_dir:
        return
    path = Path(trace_dir)
    path.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **metadata,
        **redact(payload),
    }
    with (path / "router-full.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_turn_artifact(settings: Any, metadata: dict[str, Any], name: str, payload: Any) -> None:
    if not getattr(settings, "router_trace_full", False):
        return
    trace_dir = getattr(settings, "router_trace_dir", "")
    if not trace_dir:
        return
    safe_agent = safe_path_part(str(metadata.get("agent") or "unknown"))
    safe_instance = safe_path_part(str(metadata.get("instance_id") or "unknown"))
    turn = int(metadata.get("turn_index") or 0)
    endpoint = safe_path_part(str(metadata.get("endpoint") or "unknown"))
    path = Path(trace_dir) / "conversations" / safe_agent / safe_instance / f"{turn:04d}-{endpoint}"
    path.mkdir(parents=True, exist_ok=True)
    with (path / name).open("w", encoding="utf-8") as fh:
        json.dump(redact(payload), fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def safe_path_part(value: str) -> str:
    chars = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            chars.append(char)
        else:
            chars.append("-")
    cleaned = "".join(chars).strip("-._")
    return cleaned or "unknown"
