from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AgentProtocol = Literal["openai.responses", "anthropic.messages", "openai.chat", "cursor.rpc"]
AttackArgumentFormat = Literal["codex_exec_json", "json_object"]
AgentName = str
AgentMode = str


@dataclass(frozen=True)
class AgentDefinition:
    name: AgentName
    protocol: AgentProtocol
    attack_argument_format: AttackArgumentFormat
    modes: tuple[str, ...]
    default_mode: str


# 中文注释：新增 code agent 时先在这里登记能力，再补对应容器 adapter。
AGENT_DEFINITIONS: dict[AgentName, AgentDefinition] = {
    "codex": AgentDefinition(
        name="codex",
        protocol="openai.responses",
        attack_argument_format="codex_exec_json",
        modes=("auto", "full_access"),
        default_mode="auto_review",
    ),
    "claude": AgentDefinition(
        name="claude",
        protocol="anthropic.messages",
        attack_argument_format="json_object",
        modes=("plan", "acceptEdits", "auto", "bypassPermissions"),
        default_mode="auto_review",
    ),
    "cursor": AgentDefinition(
        name="cursor",
        protocol="cursor.rpc",
        attack_argument_format="json_object",
        modes=("auto_review", "run_everything"),
        default_mode="auto_review",
    ),
    "opencode": AgentDefinition(
        name="opencode",
        protocol="openai.chat",
        attack_argument_format="json_object",
        modes=("plan", "build"),
        default_mode="plan",
    ),
}
DEFAULT_AGENT: AgentName = "codex"
SUPPORTED_AGENT_NAMES: tuple[AgentName, ...] = tuple(AGENT_DEFINITIONS)


def normalize_agent_name(value: str, *, default: AgentName = DEFAULT_AGENT) -> AgentName:
    value = "claude" if value == "claudecode" else value
    return value if value in AGENT_DEFINITIONS else default


def require_agent_name(value: str) -> AgentName:
    value = "claude" if value == "claudecode" else value
    if value not in AGENT_DEFINITIONS:
        supported = ", ".join(SUPPORTED_AGENT_NAMES)
        raise ValueError(f"unsupported agent: {value}; supported agents: {supported}")
    return value


def agent_definition(agent: AgentName) -> AgentDefinition:
    return AGENT_DEFINITIONS[require_agent_name(agent)]


def resolve_agent_mode(agent: AgentName, value: str | None) -> str:
    """Validate native modes and keep historical manifests executable."""
    definition = agent_definition(agent)
    value = value or definition.default_mode
    aliases = {
        "codex": {"auto_review": "auto", "allow_all": "full_access"},
        "claude": {"auto_review": "auto", "allow_all": "acceptEdits"},
        "cursor": {"auto-review": "auto_review", "allow_all": "run_everything"},
        "opencode": {},
    }
    mode = aliases[definition.name].get(value, value)
    if mode not in definition.modes:
        raise ValueError(f"unsupported mode {value!r} for {definition.name}; choose {', '.join(definition.modes)}")
    return mode
