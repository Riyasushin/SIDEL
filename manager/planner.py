"""Build experiment manifests for shared-router SWE-bench runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from code_agents import agent_definition, require_agent_name, resolve_agent_mode
from manager.manifest import (
    AgentMode,
    AgentName,
    ExperimentManifest,
    InjectionMode,
    InjectionTurnMode,
    ReplayMatchMode,
    RouterMode,
    api_key_hash,
    generate_router_api_key,
)
from swebench_container.images import default_swebench_image, default_wrapper_image


@dataclass(frozen=True)
class MatrixPlanOptions:
    run_id: str
    router_mode: RouterMode
    agents: tuple[AgentName, ...]
    agent_mode: AgentMode | None
    instance_ids: tuple[str, ...]
    dataset_sample_ids: tuple[str, ...]
    injection_turns: tuple[int | None, ...]
    injection_mode: InjectionMode
    payload_execution_allowed: bool
    timeout_seconds: int
    trace_source_root: Path | None = None
    replay_match_mode: ReplayMatchMode = "exact_request_hash"
    enable_tool_defense: bool = False
    dynamic_l3_enabled: bool = False
    dynamic_l4_enabled: bool = False
    injection_turn_mode: InjectionTurnMode = "fixed"
    injection_random_seed: int | None = None
    simulate_tool_results: bool = False
    max_task_turns: int | None = None


def load_task_instance_ids(tasks_path: Path, limit: int = 0, selected: Iterable[str] = ()) -> tuple[str, ...]:
    selected_set = {item for item in selected if item}
    instance_ids: list[str] = []
    with tasks_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            instance_id = str(row.get("instance_id") or "")
            if not instance_id:
                continue
            if selected_set and instance_id not in selected_set:
                continue
            instance_ids.append(instance_id)
            if limit and len(instance_ids) >= limit:
                break
    if not instance_ids:
        raise ValueError(f"no SWE-bench tasks selected from {tasks_path}")
    return tuple(instance_ids)


def load_dataset_sample_ids(dataset_path: Path, selectors: str) -> tuple[str, ...]:
    rows = load_jsonl(dataset_path)
    by_id = {str(row.get("id")): row for row in rows}
    samples: list[str] = []
    seen: set[str] = set()
    for part in (item.strip() for item in selectors.split(",") if item.strip()):
        values = expand_selector(part, len(rows))
        for value in values:
            if value.isdigit():
                sample_id = str(rows[int(value) - 1].get("id"))
            else:
                if value not in by_id:
                    raise ValueError(f"unknown dataset sample id: {value}")
                sample_id = value
            if sample_id not in seen:
                seen.add(sample_id)
                samples.append(sample_id)
    if not samples:
        raise ValueError("at least one dataset sample is required")
    return tuple(samples)


def build_matrix_plan(options: MatrixPlanOptions) -> dict[str, ExperimentManifest]:
    if options.injection_turn_mode == "first_bash" and options.router_mode != "inject":
        raise ValueError("first_bash requires inject mode")
    if options.max_task_turns is not None and options.max_task_turns < 1:
        raise ValueError("max_task_turns must be positive")
    if options.simulate_tool_results:
        if options.agents != ("claude",) or options.router_mode != "inject" or options.injection_mode != "replace":
            raise ValueError("simulated tool results require Claude inject/replace")
        if options.enable_tool_defense or options.payload_execution_allowed:
            raise ValueError("simulation requires defense disabled and raw execution unauthorized")
    keyed: dict[str, ExperimentManifest] = {}
    turns = effective_turns(options)
    sample_ids = effective_sample_ids(options)
    for agent in options.agents:
        resolve_agent_mode(agent, options.agent_mode)
        agent_mode = options.agent_mode or agent_definition(agent).default_mode
        for instance_id in options.instance_ids:
            for sample_id in sample_ids:
                for turn in turns:
                    api_key = generate_router_api_key(context_key_prefix(agent, instance_id))
                    context_id = context_id_for(options, agent, instance_id, sample_id, turn)
                    keyed[api_key] = ExperimentManifest(
                        run_id=options.run_id,
                        context_id=context_id,
                        agent=agent,
                        agent_mode=agent_mode,
                        instance_id=instance_id,
                        router_mode=options.router_mode,
                        trace_source_dir=trace_source_dir(options, instance_id, agent),
                        replay_until_turn=turn if options.router_mode in {"replay", "replay-inject"} else None,
                        injection_turn=resolved_injection_turn(options, turn),
                        dataset_sample_id=dataset_sample_id(options, sample_id),
                        injection_mode=options.injection_mode,
                        payload_execution_allowed=options.payload_execution_allowed,
                        timeout_seconds=options.timeout_seconds,
                        swebench_image=default_swebench_image(instance_id),
                        wrapper_image=default_wrapper_image(options.run_id, instance_id, agent),
                        router_api_key_hash=api_key_hash(api_key),
                        injection_turn_mode=options.injection_turn_mode,
                        injection_turns=configured_injection_turns(options),
                        injection_random_seed=options.injection_random_seed,
                        replay_match_mode=options.replay_match_mode,
                        enable_tool_defense=options.enable_tool_defense,
                        dynamic_l3_enabled=options.dynamic_l3_enabled,
                        dynamic_l4_enabled=options.dynamic_l4_enabled,
                        simulate_tool_results=options.simulate_tool_results,
                        max_task_turns=options.max_task_turns,
                    )
    return keyed


def effective_turns(options: MatrixPlanOptions) -> tuple[int | None, ...]:
    if options.router_mode == "proxy-record":
        return (None,)
    if options.injection_turn_mode in {"random", "first_bash"}:
        return (None,)
    return options.injection_turns or (None,)


def effective_sample_ids(options: MatrixPlanOptions) -> tuple[str, ...]:
    if options.router_mode in {"proxy-record", "replay"}:
        return ("record",)
    return options.dataset_sample_ids


def dataset_sample_id(options: MatrixPlanOptions, sample_id: str) -> str:
    if options.router_mode in {"proxy-record", "replay"}:
        return ""
    return sample_id


def parse_agents(value: str) -> tuple[AgentName, ...]:
    agents: list[AgentName] = []
    for item in (part.strip() for part in value.split(",") if part.strip()):
        agents.append(require_agent_name(item))
    if not agents:
        raise ValueError("at least one agent is required")
    return tuple(agents)


def parse_turns(value: str, router_mode: RouterMode) -> tuple[int | None, ...]:
    if router_mode == "proxy-record":
        return (None,)
    turns: list[int | None] = []
    for item in (part.strip() for part in value.split(",") if part.strip()):
        turns.extend(expand_turn_selector(item))
    if not turns:
        raise ValueError("replay, replay-inject, and inject require at least one turn")
    return tuple(turns)


def load_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    if not rows:
        raise ValueError(f"empty JSONL file: {path}")
    return rows


def expand_selector(selector: str, row_count: int) -> list[str]:
    if selector == "all":
        return [str(index) for index in range(1, row_count + 1)]
    if ":" not in selector:
        return [selector]
    start_s, end_s = selector.split(":", 1)
    start = int(start_s)
    end = int(end_s)
    if start < 1 or end < start or end > row_count:
        raise ValueError(f"invalid dataset selector range: {selector}")
    return [str(index) for index in range(start, end + 1)]


def expand_turn_selector(selector: str) -> list[int]:
    if ":" not in selector:
        turn = int(selector)
        if turn < 0:
            raise ValueError(f"invalid injection turn: {selector}")
        return [turn]
    start_s, end_s = selector.split(":", 1)
    start = int(start_s)
    end = int(end_s)
    if start < 0 or end < start:
        raise ValueError(f"invalid injection turn range: {selector}")
    return list(range(start, end + 1))


def resolved_injection_turn(options: MatrixPlanOptions, turn: int | None) -> int | None:
    if options.router_mode not in {"replay-inject", "inject"}:
        return None
    if options.injection_turn_mode in {"random", "first_bash"}:
        return None
    return turn


def configured_injection_turns(options: MatrixPlanOptions) -> tuple[int, ...]:
    return tuple(int(turn) for turn in options.injection_turns if turn is not None)


def trace_source_dir(options: MatrixPlanOptions, instance_id: str, agent: AgentName) -> str:
    if options.trace_source_root is None:
        return ""
    return str(options.trace_source_root / agent / safe_token(instance_id) / "router" / "traces")


def context_id_for(options: MatrixPlanOptions, agent: AgentName, instance_id: str, sample_id: str, turn: int | None) -> str:
    if turn is None:
        if options.router_mode == "proxy-record":
            turn_part = "record"
        elif options.injection_turn_mode in {"random", "first_bash"}:
            turn_part = "turn-" + options.injection_turn_mode
        else:
            turn_part = "turn-none"
    else:
        turn_part = f"turn-{turn}"
    return "-".join(
        [
            safe_token(options.run_id),
            safe_token(instance_id),
            agent,
            options.agent_mode or agent_definition(agent).default_mode,
            safe_token(sample_id),
            turn_part,
        ]
    )


def context_key_prefix(agent: AgentName, instance_id: str) -> str:
    return f"rk_{agent}_{safe_token(instance_id)[:24]}"


def safe_token(value: str) -> str:
    chars = [char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value]
    return "".join(chars).strip("-._") or "unknown"
