"""Load attack samples and render their original tool arguments.

Dataset samples render only as raw tool arguments for isolated Docker experiments.
Experiments that need reviewer behavior should inspect captured review traces.
Rendering raw dataset arguments requires explicit ATTACK_ALLOW_UNSAFE_RAW=true
because the dataset contains dangerous-looking commands and file operations.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from code_agents import AgentName, agent_definition

PayloadMode = Literal["raw"]


class AttackDatasetError(ValueError):
    pass


def load_attack_dataset(path: str) -> list[dict[str, Any]]:
    dataset_path = Path(path)
    rows: list[dict[str, Any]] = []
    with dataset_path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AttackDatasetError(f"invalid JSONL at {dataset_path}:{lineno}: {exc}") from exc
            if not isinstance(row, dict):
                raise AttackDatasetError(f"attack dataset row must be an object at {dataset_path}:{lineno}")
            rows.append(row)
    if not rows:
        raise AttackDatasetError(f"attack dataset is empty: {dataset_path}")
    return rows


@lru_cache(maxsize=8)
def cached_attack_dataset(path: str) -> tuple[dict[str, Any], ...]:
    return tuple(load_attack_dataset(path))


def select_attack_sample(path: str, selector: str) -> dict[str, Any]:
    rows = cached_attack_dataset(path)
    cleaned = selector.strip() or "1"
    if cleaned.isdigit():
        index = int(cleaned)
        if index < 1 or index > len(rows):
            raise AttackDatasetError(f"attack sample index out of range: {index}; dataset has {len(rows)} rows")
        return dict(rows[index - 1])
    for row in rows:
        if str(row.get("id")) == cleaned:
            return dict(row)
    raise AttackDatasetError(f"attack sample id not found: {cleaned}")


def attack_response_text(sample: dict[str, Any], fallback: str) -> str:
    text = sample.get("model_response_text")
    if isinstance(text, str) and text.strip():
        return text
    return fallback


def render_tool_arguments(
    *,
    sample: dict[str, Any],
    agent: AgentName,
    tool_name: str,
    payload_mode: PayloadMode,
    allow_unsafe_raw: bool,
    codex_sandbox_permissions: str = "",
    codex_approval_justification: str = "",
) -> str | dict[str, Any]:
    if payload_mode != "raw":
        raise AttackDatasetError(
            "non-raw marker payload rendering has been removed; use "
            "ATTACK_PAYLOAD_MODE=raw and inspect auto-review traces"
        )
    if not allow_unsafe_raw:
        raise AttackDatasetError(
            "raw dataset payload rendering requires ATTACK_ALLOW_UNSAFE_RAW=true; "
            "prefer review-trace analysis when testing whether reviewers would allow a payload"
        )
    args = sample.get("tool_arguments")
    if not isinstance(args, dict):
        raise AttackDatasetError(f"attack sample {sample.get('id')} has non-object tool_arguments")
    return raw_agent_arguments(
        args=args,
        agent=agent,
        tool_name=tool_name,
        codex_sandbox_permissions=codex_sandbox_permissions,
        codex_approval_justification=codex_approval_justification,
    )


def raw_agent_arguments(
    *,
    args: dict[str, Any],
    agent: AgentName,
    tool_name: str,
    codex_sandbox_permissions: str = "",
    codex_approval_justification: str = "",
) -> str | dict[str, Any]:
    definition = agent_definition(agent)
    if definition.attack_argument_format == "codex_exec_json":
        command = str(args.get("command") or args)
        codex_args: dict[str, Any] = {"cmd": command, "yield_time_ms": 1000, "max_output_tokens": 2000}
        if codex_sandbox_permissions:
            codex_args["sandbox_permissions"] = codex_sandbox_permissions
            codex_args["justification"] = codex_approval_justification or "Approve Codex auto-review experiment payload."
        return json.dumps(codex_args)
    return dict(args)
