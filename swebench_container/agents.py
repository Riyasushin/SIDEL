from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Protocol

from code_agents import AgentName, require_agent_name, resolve_agent_mode
from swebench_container.models import ContainerRunSpec


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
        mode = resolve_agent_mode(self.name, spec.agent_mode)
        router_base_url = f"{spec.router_url.rstrip('/')}/v1"
        command = [
            "codex",
            "--config",
            'model_provider="router"',
            "--config",
            f'model_providers.router={{name="router", base_url="{router_base_url}", env_key="OPENAI_API_KEY", wire_api="responses"}}',
        ]
        if mode == "auto":
            command += ["--ask-for-approval", "on-request", "--config", 'approvals_reviewer="auto_review"']
        command += ["exec", "--skip-git-repo-check"]
        command += (["--dangerously-bypass-approvals-and-sandbox"] if mode == "full_access"
                    else ["--sandbox", "workspace-write"])
        return command + ["__PROMPT_FROM_ARTIFACT__"]

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
        permission_mode = resolve_agent_mode(self.name, spec.agent_mode)
        command = ["claude", "--permission-mode", permission_mode]
        if spec.max_task_turns is not None:
            command += ["--max-turns", str(spec.max_task_turns)]
        if spec.simulate_tool_results:
            settings = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{
                "type": "command", "command": "python3 /opt/sidel/claude_simulation_hook.py || { echo 'Simulation hook failed; execution skipped' >&2; exit 2; }",
                "timeout": 10,
            }]}]}}
            command += ["--settings", json.dumps(settings)]
        return command + ["--print", "__PROMPT_FROM_ARTIFACT__"]

    def protocol_preferences(self) -> dict[str, str]:
        return {"llm_protocol": "anthropic.messages", "review_mode": "claude"}


@dataclass(frozen=True)
class OpenCodeAdapter:
    name: AgentName = "opencode"

    def build_env(self, spec: ContainerRunSpec) -> dict[str, str]:
        mode = resolve_agent_mode(self.name, spec.agent_mode)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": "sidel/router",
            "small_model": "sidel/router",
            "enabled_providers": ["sidel"],
            "autoupdate": False,
            "provider": {"sidel": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "SIDEL",
                "options": {"baseURL": f"{spec.router_url.rstrip('/')}/v1", "apiKey": "{env:ROUTER_API_KEY}"},
                "models": {"router": {"name": "Router", "limit": {"context": 128000, "output": 8192}}},
            }},
        }
        if mode == "build":
            config["permission"] = "allow"
        env = {
            "ROUTER_API_KEY": spec.router_api_key,
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "XDG_CONFIG_HOME": "/tmp/opencode-config",
            "XDG_DATA_HOME": "/tmp/opencode-data",
            "XDG_CACHE_HOME": "/tmp/opencode-cache",
        }
        env.update(spec.environment)
        return env

    def build_command(self, spec: ContainerRunSpec) -> list[str]:
        mode = resolve_agent_mode(self.name, spec.agent_mode)
        return ["opencode", "run", "--agent", mode, "--model", "sidel/router", "--format", "json", "__PROMPT_FROM_ARTIFACT__"]

    def protocol_preferences(self) -> dict[str, str]:
        return {"llm_protocol": "openai.chat", "review_mode": "opencode"}


@dataclass(frozen=True)
class CursorAdapter:
    """Cursor CLI speaks native RPC, rather than an OpenAI-compatible API."""
    name: AgentName = "cursor"

    def build_env(self, spec: ContainerRunSpec) -> dict[str, str]:
        env = {
            "CURSOR_API_ENDPOINT": spec.router_url.rstrip("/") + "/cursor",
            "CURSOR_API_KEY": spec.router_api_key,
            "CURSOR_CONFIG_DIR": "/tmp/cursor-config",
            "SIDEL_CURSOR_CONFIG": json.dumps({"version": 1, "editor": {"vimMode": False},
                "permissions": {"allow": [], "deny": []}, "network": {"useHttp1ForAgent": True}}),
        }
        env.update(spec.environment)
        return env

    def build_command(self, spec: ContainerRunSpec) -> list[str]:
        mode = resolve_agent_mode(self.name, spec.agent_mode)
        return ["cursor-agent", "--print", "--output-format", "stream-json", "--trust",
                "--auto-review" if mode == "auto_review" else "--yolo", "__PROMPT_FROM_ARTIFACT__"]

    def protocol_preferences(self) -> dict[str, str]:
        return {"llm_protocol": "cursor.rpc", "review_mode": "cursor"}


AGENT_ADAPTERS: dict[AgentName, AgentAdapter] = {
    "codex": CodexAdapter(),
    "claude": ClaudeAdapter(),
    "opencode": OpenCodeAdapter(),
    "cursor": CursorAdapter(),
}


def adapter_for(agent: AgentName) -> AgentAdapter:
    name = require_agent_name(agent)
    try:
        return AGENT_ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(f"agent {name} is registered but has no container adapter") from exc
