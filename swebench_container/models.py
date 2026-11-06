from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from code_agents import AgentMode, AgentName


@dataclass(frozen=True)
class ContainerRunSpec:
    run_id: str
    context_id: str
    instance_id: str
    agent: AgentName
    agent_mode: AgentMode
    router_url: str
    router_api_key: str
    artifact_dir: Path
    swebench_image: str
    wrapper_image: str
    issue_prompt: str
    timeout_seconds: int = 1800
    platform: str = "linux/amd64"
    network: str = "bridge"
    environment: dict[str, str] = field(default_factory=dict)
    docker_inspect_path: Path | None = None


@dataclass(frozen=True)
class ContainerRunResult:
    context_id: str
    status: str
    exit_code: int | None
    timed_out: bool
    stdout_path: Path
    stderr_path: Path
    artifact_dir: Path
