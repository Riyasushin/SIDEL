from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AgentProtocol = Literal["openai.responses", "anthropic.messages"]
AttackArgumentFormat = Literal["codex_exec_json", "json_object"]
AgentName = str


@dataclass(frozen=True)
class AgentDefinition:
    name: AgentName
    protocol: AgentProtocol
    attack_argument_format: AttackArgumentFormat


# 中文注释：新增 code agent 时先在这里登记能力，再补对应容器 adapter。
AGENT_DEFINITIONS: dict[AgentName, AgentDefinition] = {
    "codex": AgentDefinition(
        name="codex",
        protocol="openai.responses",
        attack_argument_format="codex_exec_json",
    ),
    "claude": AgentDefinition(
        name="claude",
        protocol="anthropic.messages",
        attack_argument_format="json_object",
    ),
}
DEFAULT_AGENT: AgentName = "codex"
SUPPORTED_AGENT_NAMES: tuple[AgentName, ...] = tuple(AGENT_DEFINITIONS)


def normalize_agent_name(value: str, *, default: AgentName = DEFAULT_AGENT) -> AgentName:
    return value if value in AGENT_DEFINITIONS else default


def require_agent_name(value: str) -> AgentName:
    if value not in AGENT_DEFINITIONS:
        supported = ", ".join(SUPPORTED_AGENT_NAMES)
        raise ValueError(f"unsupported agent: {value}; supported agents: {supported}")
    return value


def agent_definition(agent: AgentName) -> AgentDefinition:
    return AGENT_DEFINITIONS[require_agent_name(agent)]
