from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from router.context import ExperimentContext


@dataclass(frozen=True)
class TerminalState:
    context_key: str
    context_id: str
    reason: str
    turn_index: int
    event: str
    payload: dict[str, Any]
    created_at: str


_TERMINAL_STATES: dict[str, TerminalState] = {}
_TERMINAL_LOCK = threading.Lock()


def context_state_key(context: ExperimentContext) -> str:
    return f"{context.run_id}:{context.context_id}"


def mark_injection_completed(context: ExperimentContext, metadata: dict[str, Any], injection: dict[str, Any]) -> TerminalState:
    state = TerminalState(
        context_key=context_state_key(context),
        context_id=context.context_id,
        reason="injection_completed",
        turn_index=int(metadata.get("turn_index") or 0),
        event="router.injection_completed",
        payload={
            "dataset_sample_id": injection.get("dataset_sample_id"),
            "injection_turn": injection.get("injection_turn"),
            "tool_name": injection.get("tool_name"),
            "protocol": injection.get("protocol"),
        },
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with _TERMINAL_LOCK:
        _TERMINAL_STATES[state.context_key] = state
    return state


def mark_replay_completed(context: ExperimentContext, metadata: dict[str, Any]) -> TerminalState:
    state = TerminalState(
        context_key=context_state_key(context),
        context_id=context.context_id,
        reason="replay_completed",
        turn_index=int(metadata.get("turn_index") or 0),
        event="router.replay_completed",
        payload={
            "replay_until_turn": context.replay_until_turn,
            "task_turn_index": metadata.get("task_turn_index"),
            "endpoint": metadata.get("endpoint"),
        },
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with _TERMINAL_LOCK:
        _TERMINAL_STATES[state.context_key] = state
    return state


def terminal_state_for(context: ExperimentContext) -> TerminalState | None:
    with _TERMINAL_LOCK:
        return _TERMINAL_STATES.get(context_state_key(context))



def reset_terminal_states() -> None:
    with _TERMINAL_LOCK:
        _TERMINAL_STATES.clear()
