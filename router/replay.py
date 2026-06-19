"""Replay previously recorded router responses for one experiment context."""

import copy
import json
from pathlib import Path
from typing import Any

from router.context import ExperimentContext
from router.tracing import safe_path_part, stable_hash


class ReplayLookupError(RuntimeError):
    pass


def replay_root(context: ExperimentContext) -> Path:
    configured = Path(context.trace_source_dir or "")
    if not configured:
        raise ReplayLookupError("trace_source_dir is required for replay contexts")
    if not configured.exists():
        raise ReplayLookupError(f"replay trace directory does not exist: {configured}")
    return configured


def replay_turn_dir(context: ExperimentContext, metadata: dict[str, Any]) -> Path:
    root = replay_root(context)
    turn = int(metadata.get("turn_index") or 0)
    endpoint = safe_path_part(str(metadata.get("endpoint") or "unknown"))
    preferred = root / f"{turn:04d}-{endpoint}"
    if preferred.exists():
        return preferred

    # Historical traces used a conversations/agent/instance tree. Keep this as a
    # reader fallback so old clean records can seed new shared-router runs, while
    # all new writes use the per-context layout.
    legacy = (
        root
        / "conversations"
        / safe_path_part(context.agent)
        / safe_path_part(context.instance_id)
        / f"{turn:04d}-{endpoint}"
    )
    if legacy.exists():
        return legacy
    raise ReplayLookupError(f"missing replay turn directory: {preferred}")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplayLookupError(f"missing replay artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReplayLookupError(f"invalid replay JSON artifact: {path}: {exc}") from exc


# 严格 replay 的核心证据是当前请求 hash 与记录请求 hash 完全一致。
def load_replay_response(
    context: ExperimentContext,
    metadata: dict[str, Any],
    current_request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    turn_dir = replay_turn_dir(context, metadata)
    response_path = select_response_path(context, turn_dir)
    clean_request_path = turn_dir / "request.agent.json"
    response = read_json(response_path)
    if not isinstance(response, dict):
        raise ReplayLookupError(f"replay response must be a JSON object: {response_path}")

    clean_request = read_json(clean_request_path) if clean_request_path.exists() else None
    current_hash = stable_hash(current_request)
    expected_hash = stable_hash(clean_request) if isinstance(clean_request, dict) else ""
    replay = {
        "mode": context.router_mode,
        "context_id": context.context_id,
        "source_turn_dir": str(turn_dir),
        "source_response_path": str(response_path),
        "source_request_path": str(clean_request_path) if clean_request_path.exists() else "",
        "response_file": response_path.name,
        "actual_request_hash": current_hash,
        "expected_request_hash": expected_hash,
        "request_matched": bool(expected_hash and expected_hash == current_hash),
        "strict": context.replay_strict,
        "drift_detected": bool(expected_hash and expected_hash != current_hash),
        "turn_index": metadata.get("turn_index"),
        "endpoint": metadata.get("endpoint"),
        "agent": context.agent,
        "agent_mode": context.agent_mode,
        "instance_id": context.instance_id,
    }
    if context.replay_strict:
        if not expected_hash:
            raise ReplayLookupError("strict replay requires request.agent.json in the source trace")
        if expected_hash != current_hash:
            raise ReplayLookupError(
                "strict replay request hash mismatch for "
                f"context={context.context_id} turn={metadata.get('turn_index')} endpoint={metadata.get('endpoint')}"
            )
    return copy.deepcopy(response), replay


def select_response_path(context: ExperimentContext, turn_dir: Path) -> Path:
    configured = context.replay_response_file or "response.router.after_injection.json"
    candidates = [
        turn_dir / configured,
        turn_dir / "response.router.after_injection.json",
        turn_dir / "response.router.after.json",
    ]
    if context.router_mode == "replay-inject":
        candidates.insert(1, turn_dir / "response.router.before_injection.json")
        candidates.insert(2, turn_dir / "response.router.before.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]
