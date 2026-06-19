from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from code_agents import AgentName, require_agent_name
from swebench_container.models import AgentMode, ContainerRunSpec


# Agent 差异集中在 adapter，避免 manager/runner 到处出现 if agent == ...。
class AgentAdapter(Protocol):
    name: AgentName

    def build_env(self, spec: ContainerRunSpec) -> dict[str, str]:
        ...

    def build_command(self, spec: ContainerRunSpec) -> list[str]:
        ...

    def protocol_preferences(self) -> dict[str, str]:
        ...


@dataclass(frozen=True)
class CodexAdapter:
    name: AgentName = "codex"

    def build_env(self, spec: ContainerRunSpec) -> dict[str, str]:
        env = {
            "OPENAI_BASE_URL": f"{spec.router_url.rstrip('/')}/v1",
            "OPENAI_API_KEY": spec.router_api_key,
            "CODEX_HOME": "/tmp/codex-home",
        }
        env.update(spec.environment)
        return env

    def build_command(self, spec: ContainerRunSpec) -> list[str]:
        if spec.agent_mode == "allow_all":
            return [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "__PROMPT_FROM_ARTIFACT__",
            ]
        router_base_url = f"{spec.router_url.rstrip('/')}/v1"
        return [
            "codex",
            "--ask-for-approval",
            "on-request",
            "--config",
            'approvals_reviewer="auto_review"',
            "--config",
            'model_provider="router"',
            "--config",
            f'model_providers.router={{name="router", base_url="{router_base_url}", env_key="OPENAI_API_KEY", wire_api="responses"}}',
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "__PROMPT_FROM_ARTIFACT__",
        ]

    def protocol_preferences(self) -> dict[str, str]:
        return {"llm_protocol": "openai.responses", "review_mode": "codex"}


@dataclass(frozen=True)
class ClaudeAdapter:
    name: AgentName = "claude"

    def build_env(self, spec: ContainerRunSpec) -> dict[str, str]:
        env = {
            "ANTHROPIC_BASE_URL": spec.router_url.rstrip("/"),
            "ANTHROPIC_API_KEY": spec.router_api_key,
            "CLAUDE_CONFIG_DIR": "/tmp/claude-home",
        }
        env.update(spec.environment)
        return env

    def build_command(self, spec: ContainerRunSpec) -> list[str]:
        permission_mode = "acceptEdits" if spec.agent_mode == "allow_all" else "auto"
        return ["claude", "--permission-mode", permission_mode, "--print", "__PROMPT_FROM_ARTIFACT__"]

    def protocol_preferences(self) -> dict[str, str]:
        return {"llm_protocol": "anthropic.messages", "review_mode": "claude"}


AGENT_ADAPTERS: dict[AgentName, AgentAdapter] = {
    "codex": CodexAdapter(),
    "claude": ClaudeAdapter(),
}


def adapter_for(agent: AgentName) -> AgentAdapter:
    name = require_agent_name(agent)
    try:
        return AGENT_ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(f"agent {name} is registered but has no container adapter") from exc
