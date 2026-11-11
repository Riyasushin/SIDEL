"""Supply dataset tool results without executing injected Claude tool calls."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from router.context import ExperimentContext
from router.tracing import write_trace_event, write_turn_artifact

SIMULATED_CALL_PREFIX = "toolu_sidel_sim_"


def state_path(context: ExperimentContext) -> Path:
    return Path(context.trace_dir) / "simulated_tool_results.json"


def simulation_state(context: ExperimentContext) -> dict[str, Any] | None:
    path = state_path(context)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def register_simulated_injection(context: ExperimentContext, metadata: dict[str, Any], injection: dict[str, Any]) -> None:
    call_id = injection.get("tool_call_id")
    result = injection.get("simulated_tool_result")
    if not isinstance(call_id, str) or not call_id.startswith(SIMULATED_CALL_PREFIX) or not isinstance(result, str):
        raise ValueError("simulated injection requires a correlated tool call and dataset tool_result")
    payload = {"tool_call_id": call_id, "tool_result": result,
               "dataset_sample_id": injection.get("dataset_sample_id"),
               "injection_task_turn_index": metadata.get("task_turn_index"), "execution_mode": "simulated"}
    path = state_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    write_trace_event(context, metadata, "router.simulated_injection", {"tool_call_id": call_id, "execution_mode": "simulated"})


def prepare_simulated_request(body: dict[str, Any], context: ExperimentContext, metadata: dict[str, Any]) -> dict[str, Any]:
    """Replace only the matching result in every history copy, including retries.

    The original request remains in request.agent.json. Keeping replacement
    idempotent is essential: Claude resends its local history on every turn.
    """
    if not context.simulate_tool_results or metadata.get("request_role") != "agent_main":
        return body
    if metadata.get("endpoint") != "anthropic.messages":
        raise HTTPException(400, "simulated tool results require the Anthropic messages protocol")
    state = simulation_state(context)
    if state is None:
        return body
    updated = copy.deepcopy(body)
    call_id = state["tool_call_id"]
    matched = 0
    for message in updated.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") == call_id:
                block["content"] = state["tool_result"]
                block["is_error"] = False
                matched += 1
    # The immediate continuation must acknowledge the injected call. Later
    # context compaction is allowed to remove the already-consumed pair.
    if metadata.get("task_turn_index") == state["injection_task_turn_index"] + 1 and not matched:
        raise HTTPException(400, "missing tool result for the simulated injected call")
    if matched:
        write_turn_artifact(context, metadata, "request.simulated.json", updated)
        write_turn_artifact(context, metadata, "tool-result-simulation.json", {
            "tool_call_id": call_id, "dataset_sample_id": state["dataset_sample_id"],
            "matched_results": matched, "executed": False, "execution_mode": "simulated",
        })
    return updated
