"""Replay previously recorded router responses for one experiment context."""

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable

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


def replay_turn_roots(context: ExperimentContext) -> tuple[Path, ...]:
    root = replay_root(context)
    roots = [root]
    legacy = root / "conversations" / safe_path_part(context.agent) / safe_path_part(context.instance_id)
    if legacy.exists():
        roots.append(legacy)
    return tuple(roots)


def iter_replay_turn_dirs(context: ExperimentContext) -> Iterable[Path]:
    seen: set[Path] = set()
    for root in replay_turn_roots(context):
        for path in sorted(root.iterdir()):
            if path in seen or not path.is_dir() or re.match(r"^\d+-", path.name) is None:
                continue
            seen.add(path)
            yield path


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplayLookupError(f"missing replay artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReplayLookupError(f"invalid replay JSON artifact: {path}: {exc}") from exc


def read_turn_metadata(turn_dir: Path) -> dict[str, Any]:
    payload = read_json(turn_dir / "metadata.json")
    if not isinstance(payload, dict):
        raise ReplayLookupError(f"invalid replay metadata artifact: {turn_dir / 'metadata.json'}")
    return payload


def extract_request_tool_names(endpoint: str, request_body: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tools = request_body.get("tools")
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if endpoint in {"openai.chat", "openai.responses"}:
            function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = function.get("name")
        else:
            name = tool.get("name")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def extract_response_tool_names(endpoint: str, response: dict[str, Any]) -> list[str]:
    names: list[str] = []
    if endpoint == "anthropic.messages":
        blocks = response.get("content")
        if not isinstance(blocks, list):
            return names
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if isinstance(name, str) and name and name not in names:
                names.append(name)
        return names

    if endpoint == "openai.responses":
        for item in response.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                name = item.get("name")
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
                continue
            for tool_call in item.get("tool_calls", []) or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                name = function.get("name") or tool_call.get("name")
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
        return names

    if endpoint == "openai.chat":
        for choice in response.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            for tool_call in message.get("tool_calls", []) or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                name = function.get("name") or tool_call.get("name")
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
        return names
    return names


def build_replay_record(
    *,
    context: ExperimentContext,
    metadata: dict[str, Any],
    turn_dir: Path,
    response_path: Path,
    clean_request_path: Path,
    current_hash: str,
    expected_hash: str,
    matched_by: str,
    matched_source_turn_index: int,
    compatibility_passed: bool,
    source_response_tool_names: list[str],
    missing_live_tool_names: list[str],
    live_tool_count: int,
    source_tool_count: int,
) -> dict[str, Any]:
    return {
        "mode": context.router_mode,
        "match_mode": context.replay_match_mode,
        "context_id": context.context_id,
        "source_turn_dir": str(turn_dir),
        "source_response_path": str(response_path),
        "source_request_path": str(clean_request_path) if clean_request_path.exists() else "",
        "response_file": response_path.name,
        "actual_request_hash": current_hash,
        "expected_request_hash": expected_hash,
        "request_matched": bool(expected_hash and expected_hash == current_hash),
        "request_hash_matched": bool(expected_hash and expected_hash == current_hash),
        "strict": context.replay_strict,
        "drift_detected": bool(expected_hash and expected_hash != current_hash),
        "turn_index": metadata.get("turn_index"),
        "task_turn_index": metadata.get("task_turn_index"),
        "matched_by": matched_by,
        "matched_source_turn_index": matched_source_turn_index,
        "compatibility_passed": compatibility_passed,
        "source_response_tool_names": source_response_tool_names,
        "missing_live_tool_names": missing_live_tool_names,
        "live_tool_count": live_tool_count,
        "source_tool_count": source_tool_count,
        "endpoint": metadata.get("endpoint"),
        "agent": context.agent,
        "agent_mode": context.agent_mode,
        "instance_id": context.instance_id,
    }


def load_exact_replay_response(
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
    source_turn_metadata = read_turn_metadata(turn_dir)
    replay = build_replay_record(
        context=context,
        metadata=metadata,
        turn_dir=turn_dir,
        response_path=response_path,
        clean_request_path=clean_request_path,
        current_hash=current_hash,
        expected_hash=expected_hash,
        matched_by="turn_index",
        matched_source_turn_index=int(source_turn_metadata.get("turn_index") or metadata.get("turn_index") or 0),
        compatibility_passed=True,
        source_response_tool_names=extract_response_tool_names(str(metadata.get("endpoint") or ""), response),
        missing_live_tool_names=[],
        live_tool_count=len(extract_request_tool_names(str(metadata.get("endpoint") or ""), current_request)),
        source_tool_count=len(extract_request_tool_names(str(metadata.get("endpoint") or ""), clean_request if isinstance(clean_request, dict) else {})),
    )
    if context.replay_strict:
        if not expected_hash:
            raise ReplayLookupError("strict replay requires request.agent.json in the source trace")
        if expected_hash != current_hash:
            raise ReplayLookupError(
                "strict replay request hash mismatch for "
                f"context={context.context_id} turn={metadata.get('turn_index')} endpoint={metadata.get('endpoint')}"
            )
    return copy.deepcopy(response), replay


def select_task_turn_dir(context: ExperimentContext, metadata: dict[str, Any]) -> Path:
    task_turn_index = metadata.get("task_turn_index")
    endpoint = str(metadata.get("endpoint") or "")
    request_role = str(metadata.get("request_role") or "agent_main")
    if task_turn_index is None:
        raise ReplayLookupError(
            "task-turn compatible replay requires task_turn_index for "
            f"context={context.context_id} turn={metadata.get('turn_index')} endpoint={endpoint}"
        )
    candidates: list[tuple[int, Path]] = []
    for turn_dir in iter_replay_turn_dirs(context):
        metadata_path = turn_dir / "metadata.json"
        if not metadata_path.exists():
            continue
        source_metadata = read_turn_metadata(turn_dir)
        if str(source_metadata.get("endpoint") or "") != endpoint:
            continue
        if str(source_metadata.get("request_role") or "agent_main") != request_role:
            continue
        if source_metadata.get("task_turn_index") != task_turn_index:
            continue
        candidates.append((int(source_metadata.get("turn_index") or 0), turn_dir))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
    raise ReplayLookupError(
        "task-turn compatible replay missing source turn for "
        f"context={context.context_id} task_turn={task_turn_index} endpoint={endpoint}"
    )


def load_task_turn_compatible_replay_response(
    context: ExperimentContext,
    metadata: dict[str, Any],
    current_request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    task_turn_index = metadata.get("task_turn_index")
    if metadata.get("request_role") != "agent_main" or task_turn_index is None:
        return load_exact_replay_response(context, metadata, current_request)

    turn_dir = select_task_turn_dir(context, metadata)
    source_turn_metadata = read_turn_metadata(turn_dir)
    if str(source_turn_metadata.get("endpoint") or "") != str(metadata.get("endpoint") or ""):
        raise ReplayLookupError(
            "task-turn compatible replay endpoint mismatch for "
            f"context={context.context_id} task_turn={task_turn_index} endpoint={metadata.get('endpoint')}"
        )

    response_path = select_response_path(context, turn_dir)
    clean_request_path = turn_dir / "request.agent.json"
    response = read_json(response_path)
    if not isinstance(response, dict):
        raise ReplayLookupError(f"replay response must be a JSON object: {response_path}")

    clean_request = read_json(clean_request_path) if clean_request_path.exists() else None
    current_hash = stable_hash(current_request)
    expected_hash = stable_hash(clean_request) if isinstance(clean_request, dict) else ""
    source_response_tool_names = extract_response_tool_names(str(metadata.get("endpoint") or ""), response)
    live_tool_names = extract_request_tool_names(str(metadata.get("endpoint") or ""), current_request)
    live_tool_name_set = set(live_tool_names)
    missing_live_tool_names = [name for name in source_response_tool_names if name not in live_tool_name_set]
    compatibility_passed = not missing_live_tool_names
    replay = build_replay_record(
        context=context,
        metadata=metadata,
        turn_dir=turn_dir,
        response_path=response_path,
        clean_request_path=clean_request_path,
        current_hash=current_hash,
        expected_hash=expected_hash,
        matched_by="task_turn_index",
        matched_source_turn_index=int(source_turn_metadata.get("turn_index") or 0),
        compatibility_passed=compatibility_passed,
        source_response_tool_names=source_response_tool_names,
        missing_live_tool_names=missing_live_tool_names,
        live_tool_count=len(live_tool_names),
        source_tool_count=len(extract_request_tool_names(str(metadata.get("endpoint") or ""), clean_request if isinstance(clean_request, dict) else {})),
    )
    if missing_live_tool_names and context.replay_strict:
        missing = ",".join(missing_live_tool_names)
        raise ReplayLookupError(
            "task-turn compatible replay incompatible tools for "
            f"context={context.context_id} task_turn={task_turn_index} endpoint={metadata.get('endpoint')} missing_live_tools={missing}"
        )
    return copy.deepcopy(response), replay


# 严格 replay 的核心证据是当前请求 hash 与记录请求 hash 完全一致。
def load_replay_response(
    context: ExperimentContext,
    metadata: dict[str, Any],
    current_request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if context.replay_match_mode == "task_turn_compatible":
        return load_task_turn_compatible_replay_response(context, metadata, current_request)
    return load_exact_replay_response(context, metadata, current_request)


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
