from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from router.context import ExperimentContext
from router.session_state import mark_injection_completed, terminal_state_for
from router.tracing import extract_trace_metadata, stable_hash, write_trace_event, write_turn_artifact

REPLAY_TURN_ENDPOINTS = frozenset({"openai.chat", "openai.responses", "anthropic.messages"})
AUXILIARY_ENDPOINTS = frozenset({"openai.models", "anthropic.count_tokens", "router.health", "router.debug"})


def is_replay_turn_endpoint(endpoint: str) -> bool:
    return endpoint in REPLAY_TURN_ENDPOINTS


def is_auxiliary_endpoint(endpoint: str) -> bool:
    return endpoint in AUXILIARY_ENDPOINTS


def begin_replay_turn(endpoint: str, body: dict[str, Any], context: ExperimentContext) -> dict[str, Any]:
    if not is_replay_turn_endpoint(endpoint):
        raise ValueError(f"endpoint does not consume replay turns: {endpoint}")
    metadata = extract_trace_metadata(body, context, endpoint)
    write_trace_event(
        context,
        metadata,
        "router.request",
        {
            "model": body.get("model"),
            "stream": body.get("stream"),
            "tool_choice": body.get("tool_choice"),
            "tools_count": len(body.get("tools", [])) if isinstance(body.get("tools"), list) else None,
        },
    )
    write_turn_artifact(context, metadata, "metadata.json", metadata)
    write_turn_artifact(context, metadata, "manifest.json", context.public_metadata())
    write_turn_artifact(context, metadata, "request.agent.json", body)
    stop_if_context_terminal(context, metadata)
    return metadata


def stop_if_context_terminal(context: ExperimentContext, metadata: dict[str, Any]) -> None:
    state = terminal_state_for(context)
    if state is None:
        return
    if metadata.get("request_role") == "reviewer":
        payload = {
            "status": "context_terminal_bypassed",
            "reason": state.reason,
            "context_id": context.context_id,
            "terminal_turn_index": state.turn_index,
            "current_turn_index": metadata.get("turn_index"),
            "terminal_event": state.event,
            "terminal_payload": state.payload,
        }
        # 中文注释：注入后仍允许 reviewer 请求通过，否则无法观测自动审批是否启动和放行。
        write_turn_artifact(context, metadata, "terminal-bypass.json", payload)
        write_trace_event(context, metadata, "router.terminal_bypass", payload)
        return
    payload = {
        "status": "context_terminal",
        "reason": state.reason,
        "context_id": context.context_id,
        "terminal_turn_index": state.turn_index,
        "current_turn_index": metadata.get("turn_index"),
        "terminal_event": state.event,
        "terminal_payload": state.payload,
    }
    # 中文注释：请求已先落盘；到达注入后的停止 step 后直接通知 agent 停止，不再 proxy/replay。
    write_turn_artifact(context, metadata, "terminal.json", payload)
    write_trace_event(context, metadata, "router.terminal_stop", payload)
    raise HTTPException(status_code=409, detail=payload)


def finish_replay_turn(
    context: ExperimentContext,
    metadata: dict[str, Any],
    response: dict[str, Any],
    *,
    before_injection: dict[str, Any] | None = None,
    injection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finished_at = datetime.now(timezone.utc).isoformat()
    metadata.update({"finished_at": finished_at, "response_hash": stable_hash(response)})
    if before_injection is not None:
        write_turn_artifact(context, metadata, "response.router.before_injection.json", before_injection)
    write_turn_artifact(context, metadata, "response.router.after_injection.json", response)
    if injection is not None:
        write_turn_artifact(context, metadata, "injection.json", injection)
        mark_injection_completed(context, metadata, injection)
    write_turn_artifact(context, metadata, "metadata.json", metadata)
    write_trace_event(
        context,
        metadata,
        "router.response",
        {
            "injection": injection,
            "response_type": response.get("object") or response.get("type"),
            "stop_reason": response.get("stop_reason"),
            "response_hash": metadata["response_hash"],
        },
    )
    return response
