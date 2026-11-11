from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from code_agents import AgentMode, AgentName

RouterMode = Literal["proxy-record", "replay", "replay-inject", "inject"]
InjectionMode = Literal["append", "replace"]
InjectionTurnMode = Literal["fixed", "random", "first_bash"]
ReplayMatchMode = Literal["exact_request_hash", "task_turn_compatible"]


@dataclass(frozen=True)
class ExperimentManifest:
    run_id: str
    context_id: str
    agent: AgentName
    agent_mode: AgentMode
    instance_id: str
    router_mode: RouterMode
    trace_source_dir: str
    replay_until_turn: int | None
    injection_turn: int | None
    dataset_sample_id: str
    injection_mode: InjectionMode
    payload_execution_allowed: bool
    timeout_seconds: int
    swebench_image: str
    wrapper_image: str
    router_api_key_hash: str
    injection_turn_mode: InjectionTurnMode = "fixed"
    injection_turns: tuple[int, ...] = ()
    injection_random_seed: int | None = None
    schema_version: int = 1
    replay_match_mode: ReplayMatchMode = "exact_request_hash"
    enable_tool_defense: bool = False
    dynamic_l3_enabled: bool = False
    dynamic_l4_enabled: bool = False
    simulate_tool_results: bool = False
    max_task_turns: int | None = None


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    router_mode: RouterMode
    contexts: dict[str, ExperimentManifest]
    schema_version: int = 1


def generate_router_api_key(prefix: str = "rk") -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def api_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


# 只有 registry 文件保存原始 API key；各 context manifest 只保存 key hash。
def context_registry_payload(run_id: str, router_mode: RouterMode, keyed_manifests: dict[str, ExperimentManifest]) -> dict[str, object]:
    return {
        "run_id": run_id,
        "router_mode": router_mode,
        "contexts": {api_key: router_context(manifest) for api_key, manifest in keyed_manifests.items()},
    }


def router_context(manifest: ExperimentManifest) -> dict[str, object]:
    data = asdict(manifest)
    data.pop("router_api_key_hash", None)
    data.pop("swebench_image", None)
    data.pop("wrapper_image", None)
    data.pop("schema_version", None)
    return data


def write_manifests(root: Path, keyed_manifests: dict[str, ExperimentManifest], router_mode: RouterMode) -> Path:
    if not keyed_manifests:
        raise ValueError("at least one experiment manifest is required")
    run_id = next(iter(keyed_manifests.values())).run_id
    run_root = root / run_id
    for manifest in keyed_manifests.values():
        context_dir = run_root / "experiments" / manifest.context_id
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    router_dir = run_root / "router"
    router_dir.mkdir(parents=True, exist_ok=True)
    registry_path = router_dir / "effective_config.json"
    registry_path.write_text(
        json.dumps(context_registry_payload(run_id, router_mode, keyed_manifests), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return registry_path
