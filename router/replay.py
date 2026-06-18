"""Replay previously recorded router responses.

Replay mode is intentionally prefix-based, not session-state based. The real
agent CLI still starts normally and sends request N only if its own previous
steps lead there. When request N arrives, the router loads the response recorded
for the same agent / SWE-bench instance / endpoint / turn index and returns it
instead of calling the upstream provider. This means replay can accelerate a
known prefix, but it cannot force an agent to jump to a turn it never emits.
"""

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from router.tracing import redact, safe_path_part

_TRACE_TAG_RE = re.compile(r"\[router-trace\]\n.*?\[/router-trace\]", re.DOTALL)
_DYNAMIC_KEYS = {
    "id",
    "trace_id",
    "request_id",
    "created",
    "created_at",
    "ts",
}


class ReplayLookupError(RuntimeError):
    pass


def replay_root(settings: Any) -> Path:
    configured = Path(str(getattr(settings, "router_replay_trace_dir", "") or ""))
    if not configured:
        raise ReplayLookupError("ROUTER_REPLAY_TRACE_DIR is required when ROUTER_MODE=replay")
    if (configured / "router-traces" / "conversations").exists():
        return configured / "router-traces"
    if (configured / "conversations").exists():
        return configured
    raise ReplayLookupError(f"replay trace directory has no conversations tree: {configured}")


def replay_turn_dir(settings: Any, metadata: dict[str, Any]) -> Path:
    root = replay_root(settings)
    agent = safe_path_part(str(metadata.get("agent") or "unknown"))
    instance_id = safe_path_part(str(metadata.get("instance_id") or "unknown"))
    # turn_index is the router request/response count for this agent+task, not a
    # SWE-bench test step and not an internal model reasoning step.
    turn = int(metadata.get("turn_index") or 0)
    endpoint = safe_path_part(str(metadata.get("endpoint") or "unknown"))
    return root / "conversations" / agent / instance_id / f"{turn:04d}-{endpoint}"


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplayLookupError(f"missing replay artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReplayLookupError(f"invalid replay JSON artifact: {path}: {exc}") from exc


def load_replay_response(settings: Any, metadata: dict[str, Any], current_request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    turn_dir = replay_turn_dir(settings, metadata)
    response_file = str(getattr(settings, "router_replay_response_file", "response.router.after.json") or "response.router.after.json")
    response_path = turn_dir / response_file
    # Most replay experiments should use response.router.before.json when the
    # source run already had an attack appended. Falling back to after.json keeps
    # old clean traces usable, but makes the source_response_path warning worth
    # checking in analysis output.
    if not response_path.exists() and response_file == "response.router.before.json":
        response_path = turn_dir / "response.router.after.json"
    clean_request_path = turn_dir / "request.agent.json"
    response = read_json(response_path)
    if not isinstance(response, dict):
        raise ReplayLookupError(f"replay response must be a JSON object: {response_path}")
    clean_request = read_json(clean_request_path) if clean_request_path.exists() else None
    current_hash = stable_hash(current_request)
    clean_hash = stable_hash(clean_request) if isinstance(clean_request, dict) else ""
    replay = {
        "mode": "replay-prefix",
        "source_turn_dir": str(turn_dir),
        "source_response_path": str(response_path),
        "source_request_path": str(clean_request_path) if clean_request_path.exists() else "",
        "response_file": response_file,
        "request_hash": current_hash,
        "clean_request_hash": clean_hash,
        "request_matched": bool(clean_hash and clean_hash == current_hash),
        "strict": bool(getattr(settings, "router_replay_strict", True)),
        "turn_index": metadata.get("turn_index"),
        "endpoint": metadata.get("endpoint"),
        "agent": metadata.get("agent"),
        "instance_id": metadata.get("instance_id"),
    }
    if replay["strict"] and clean_hash and clean_hash != current_hash:
        # This is a drift warning, not a hard failure. It tells the experimenter
        # that the live CLI request differs from the recorded prefix request, so
        # the run is a smoke replay rather than a high-fidelity equivalence run.
        replay["warning"] = "current request hash differs from clean replay request"
    return copy.deepcopy(response), replay


def stable_hash(value: Any) -> str:
    normalized = normalize_for_hash(redact(value))
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_for_hash(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in _DYNAMIC_KEYS:
                continue
            out[key] = normalize_for_hash(item)
        return out
    if isinstance(value, list):
        return [normalize_for_hash(item) for item in value]
    if isinstance(value, str):
        return _TRACE_TAG_RE.sub("[router-trace]\nRUN_ID=<run>\nAGENT=<agent>\nINSTANCE_ID=<instance>\n[/router-trace]", value)
    return value
