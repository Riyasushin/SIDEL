"""Experiment contexts for the shared router.

The API key is both authentication material and the routing key.  The raw key is
kept in memory only; persisted metadata uses a short SHA-256 fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Header, HTTPException

from code_agents import AgentName, normalize_agent_name
from router.config import Settings


RouterMode = Literal["proxy-record", "replay", "replay-inject"]
InjectionMode = Literal["append", "replace"]
AgentMode = Literal["allow_all", "auto_review", "unknown"]


@dataclass(frozen=True)
class ExperimentContext:
    run_id: str
    context_id: str
    api_key: str
    agent: AgentName
    agent_mode: AgentMode
    instance_id: str
    router_mode: RouterMode
    trace_dir: str
    trace_source_dir: str
    replay_until_turn: int | None
    injection_turn: int | None
    dataset_sample_id: str
    injection_mode: InjectionMode
    payload_execution_allowed: bool
    timeout_seconds: int
    replay_strict: bool
    replay_response_file: str

    @property
    def api_key_hash(self) -> str:
        return api_key_fingerprint(self.api_key)

    def public_metadata(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "context_id": self.context_id,
            "agent": self.agent,
            "agent_mode": self.agent_mode,
            "instance_id": self.instance_id,
            "router_mode": self.router_mode,
            "trace_source_dir": self.trace_source_dir,
            "replay_until_turn": self.replay_until_turn,
            "injection_turn": self.injection_turn,
            "dataset_sample_id": self.dataset_sample_id,
            "injection_mode": self.injection_mode,
            "payload_execution_allowed": self.payload_execution_allowed,
            "timeout_seconds": self.timeout_seconds,
            "router_api_key_hash": self.api_key_hash,
            "schema_version": 1,
        }


def api_key_fingerprint(api_key: str) -> str:
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return digest[:16]


# API key 在这里不是普通鉴权字段，而是实验上下文的路由键。
class ContextRegistry:
    def __init__(self, contexts: list[ExperimentContext]) -> None:
        if not contexts:
            raise ValueError("router context registry must contain at least one context")
        self._by_key = {context.api_key: context for context in contexts if context.api_key}
        self._contexts = tuple(contexts)

    @property
    def contexts(self) -> tuple[ExperimentContext, ...]:
        return self._contexts

    def resolve(self, api_key: str | None) -> ExperimentContext:
        if api_key and api_key in self._by_key:
            return self._by_key[api_key]
        if len(self._contexts) == 1 and (not self._by_key or not api_key):
            return self._contexts[0]
        raise HTTPException(status_code=401, detail="invalid router api key")


def load_context_registry(settings: Settings) -> ContextRegistry:
    if settings.router_context_registry:
        config_path = Path(settings.router_context_registry)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return ContextRegistry(contexts_from_payload(payload, settings))
    return ContextRegistry([single_context_from_settings(settings)])


def contexts_from_payload(payload: dict[str, Any], settings: Settings) -> list[ExperimentContext]:
    run_id = str(payload.get("run_id") or settings.router_trace_run_id or "local")
    default_mode = normalize_router_mode(str(payload.get("router_mode") or settings.router_mode))
    raw_contexts = payload.get("contexts")
    if not isinstance(raw_contexts, dict) or not raw_contexts:
        raise ValueError("router context registry requires a non-empty 'contexts' object")
    contexts: list[ExperimentContext] = []
    for api_key, raw_context in raw_contexts.items():
        if not isinstance(raw_context, dict):
            raise ValueError(f"context for API key fingerprint {api_key_fingerprint(str(api_key))} must be an object")
        context_id = str(raw_context.get("context_id") or raw_context.get("id") or api_key_fingerprint(str(api_key)))
        trace_dir = str(raw_context.get("trace_dir") or default_context_trace_dir(settings, run_id, context_id))
        contexts.append(
            ExperimentContext(
                run_id=run_id,
                context_id=context_id,
                api_key=str(api_key),
                agent=normalize_agent_name(str(raw_context.get("agent") or "codex")),
                agent_mode=normalize_agent_mode(str(raw_context.get("agent_mode") or "unknown")),
                instance_id=str(raw_context.get("instance_id") or "unknown"),
                router_mode=normalize_router_mode(str(raw_context.get("router_mode") or default_mode)),
                trace_dir=trace_dir,
                trace_source_dir=str(raw_context.get("trace_source_dir") or settings.router_replay_trace_dir),
                replay_until_turn=optional_int(raw_context.get("replay_until_turn")),
                injection_turn=optional_int(raw_context.get("injection_turn")),
                dataset_sample_id=str(raw_context.get("dataset_sample_id") or settings.attack_sample),
                injection_mode=normalize_injection_mode(str(raw_context.get("injection_mode") or settings.attack_injection_mode or "append")),
                payload_execution_allowed=bool(raw_context.get("payload_execution_allowed", settings.attack_allow_unsafe_raw)),
                timeout_seconds=int(raw_context.get("timeout_seconds") or settings.context_timeout_seconds),
                replay_strict=bool(raw_context.get("replay_strict", settings.router_replay_strict)),
                replay_response_file=str(raw_context.get("replay_response_file") or settings.router_replay_response_file),
            )
        )
    return contexts


# 单上下文模式只用于本地调试；正式批量实验应使用 ROUTER_CONTEXT_REGISTRY。
def single_context_from_settings(settings: Settings) -> ExperimentContext:
    run_id = settings.router_trace_run_id or "local"
    context_id = settings.router_context_id or f"{run_id}-{settings.router_trace_agent or 'agent'}-{settings.router_trace_instance_id or 'unknown'}"
    return ExperimentContext(
        run_id=run_id,
        context_id=context_id,
        api_key=settings.router_api_key,
        agent=normalize_agent_name(settings.router_trace_agent or "codex"),
        agent_mode=normalize_agent_mode(settings.router_agent_mode),
        instance_id=settings.router_trace_instance_id or "unknown",
        router_mode=normalize_router_mode(settings.router_mode),
        trace_dir=settings.router_trace_dir or default_context_trace_dir(settings, run_id, context_id),
        trace_source_dir=settings.router_replay_trace_dir,
        replay_until_turn=settings.router_replay_until_turn,
        injection_turn=settings.attack_injection_turn,
        dataset_sample_id=settings.attack_sample,
        injection_mode=normalize_injection_mode(settings.attack_injection_mode if settings.attack_injection_mode != "off" else "append"),
        payload_execution_allowed=settings.attack_allow_unsafe_raw,
        timeout_seconds=settings.context_timeout_seconds,
        replay_strict=settings.router_replay_strict,
        replay_response_file=settings.router_replay_response_file,
    )


def default_context_trace_dir(settings: Settings, run_id: str, context_id: str) -> str:
    root = settings.router_artifact_root or "artifacts/runs"
    return str(Path(root) / run_id / "experiments" / context_id / "router" / "traces")


def normalize_router_mode(value: str) -> RouterMode:
    aliases = {"proxy": "proxy-record"}
    normalized = aliases.get(value, value)
    if normalized not in {"proxy-record", "replay", "replay-inject"}:
        raise ValueError(f"unsupported router mode for shared router: {value}")
    return normalized  # type: ignore[return-value]


def normalize_injection_mode(value: str) -> InjectionMode:
    if value not in {"append", "replace"}:
        raise ValueError(f"unsupported injection mode: {value}")
    return value  # type: ignore[return-value]


def normalize_agent_mode(value: str) -> AgentMode:
    if value not in {"allow_all", "auto_review", "unknown"}:
        return "unknown"
    return value  # type: ignore[return-value]


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def extract_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    api_key: str | None = Header(default=None, alias="api-key"),
    openai_api_key: str | None = Header(default=None, alias="openai-api-key"),
) -> str | None:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    return x_api_key or api_key or openai_api_key
