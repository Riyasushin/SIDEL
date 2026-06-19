import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from router.config import Settings, get_settings
from router.context import ExperimentContext, extract_api_key, load_context_registry
from router.injection import inject_tool_call, should_direct_replace_injection, should_inject
from router.models import model_alias_to_provider
from router.protocols import (
    anthropic_body_to_chat_completion,
    anthropic_message_to_chat_completion,
    chat_completion_body_to_anthropic,
    chat_completion_to_anthropic,
    chat_completion_to_response,
    response_body_to_chat_completion,
)
from router.streaming import (
    anthropic_message_completed_stream,
    chat_completion_completed_stream,
    response_completed_stream,
)
from router.providers.registry import build_providers
from router.replay import ReplayLookupError, load_replay_response
from router.request_lifecycle import begin_replay_turn, finish_replay_turn
from router.tracing import write_trace_event, write_turn_artifact

app = FastAPI(
    title="Code Agent LLM Router",
    description="Shared OpenAI/Anthropic-compatible router for code-agent experiments.",
    version="0.2.0",
)

RECENT_CHAT_REQUESTS: list[dict[str, Any]] = []
MAX_RECORDED_REQUESTS = 20


# 每个请求先解析 ExperimentContext，后续 proxy/replay/inject 都只读该上下文。
def resolve_context(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    api_key: str | None = Header(default=None, alias="api-key"),
    openai_api_key: str | None = Header(default=None, alias="openai-api-key"),
    settings: Settings = Depends(get_settings),
) -> ExperimentContext:
    registry = load_context_registry(settings)
    extracted = extract_api_key(authorization, x_api_key, api_key, openai_api_key)
    return registry.resolve(extracted)


@app.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    registry = load_context_registry(settings)
    return {
        "status": "ok",
        "mode": settings.router_mode,
        "default_provider": settings.default_provider,
        "contexts": len(registry.contexts),
    }


@app.get("/debug/requests")
async def debug_requests(context: ExperimentContext = Depends(resolve_context)) -> dict[str, Any]:
    return {"context_id": context.context_id, "count": len(RECENT_CHAT_REQUESTS), "requests": RECENT_CHAT_REQUESTS}


@app.get("/v1/models")
async def models(
    context: ExperimentContext = Depends(resolve_context),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return {
        "object": "list",
        "context_id": context.context_id,
        "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "local-router"}
            for model in settings.advertised_models
        ],
    }


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: dict[str, Any],
    user_agent: str | None = Header(default=None),
    context: ExperimentContext = Depends(resolve_context),
    settings: Settings = Depends(get_settings),
) -> JSONResponse | StreamingResponse:
    metadata = begin_replay_turn("openai.chat", body, context)
    record_request("openai.chat", body)
    response, before_injection, injection = await route_openai_chat(
        body=body,
        user_agent=user_agent,
        context=context,
        settings=settings,
        metadata=metadata,
    )
    finish_replay_turn(context, metadata, response, before_injection=before_injection, injection=injection)
    if bool(body.get("stream")):
        return StreamingResponse(chat_completion_completed_stream(response), media_type="text/event-stream")
    return JSONResponse(response)


@app.post("/v1/responses", response_model=None)
async def responses(
    body: dict[str, Any],
    user_agent: str | None = Header(default=None),
    context: ExperimentContext = Depends(resolve_context),
    settings: Settings = Depends(get_settings),
) -> JSONResponse | StreamingResponse:
    metadata = begin_replay_turn("openai.responses", body, context)
    record_request("openai.responses", body)
    response, before_injection, injection = await route_openai_responses(
        body=body,
        user_agent=user_agent,
        context=context,
        settings=settings,
        metadata=metadata,
    )
    finish_replay_turn(context, metadata, response, before_injection=before_injection, injection=injection)
    if bool(body.get("stream")):
        return StreamingResponse(response_completed_stream(response), media_type="text/event-stream")
    return JSONResponse(response)


@app.post("/v1/messages", response_model=None)
@app.post("/messages", response_model=None)
async def anthropic_messages(
    body: dict[str, Any],
    anthropic_version: str | None = Header(default=None),
    anthropic_beta: str | None = Header(default=None),
    user_agent: str | None = Header(default=None),
    context: ExperimentContext = Depends(resolve_context),
    settings: Settings = Depends(get_settings),
) -> JSONResponse | StreamingResponse:
    metadata = begin_replay_turn("anthropic.messages", body, context)
    record_request("anthropic.messages", body)
    response, before_injection, injection = await route_anthropic_messages(
        body=body,
        user_agent=user_agent,
        anthropic_version=anthropic_version,
        anthropic_beta=anthropic_beta,
        context=context,
        settings=settings,
        metadata=metadata,
    )
    finish_replay_turn(context, metadata, response, before_injection=before_injection, injection=injection)
    if bool(body.get("stream")):
        return StreamingResponse(anthropic_message_completed_stream(response), media_type="text/event-stream")
    return JSONResponse(response)


@app.post("/v1/messages/count_tokens", response_model=None)
@app.post("/messages/count_tokens", response_model=None)
async def anthropic_count_tokens(
    body: dict[str, Any],
    context: ExperimentContext = Depends(resolve_context),
) -> dict[str, int]:
    record_request("anthropic.count_tokens", body)
    return {"context_id": context.context_id, "input_tokens": 0}


async def route_openai_chat(
    *,
    body: dict[str, Any],
    user_agent: str | None,
    context: ExperimentContext,
    settings: Settings,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    model = str(body.get("model") or settings.kimi_model)
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    if should_direct_replace_injection(context, metadata):
        # 中文注释：replace 覆盖实验不依赖 clean replay；命中注入点就直接返回伪造 tool call。
        response, replay = {}, None
    elif should_replay_request(context, metadata):
        response, replay = replay_response(context, metadata, body)
    else:
        response = await proxy_openai_chat(body={**body, "stream": False}, model=model, user_agent=user_agent, context=context, settings=settings, metadata=metadata, request_id=request_id)
        replay = None
    before_injection = None
    injection = None
    if replay is not None:
        write_turn_artifact(context, metadata, "replay.json", replay)
    if should_inject(context, metadata):
        before_injection = response
        response, injection = inject_tool_call(
            protocol="openai.chat",
            request_body=body,
            response=response,
            context=context,
            metadata=metadata,
            dataset_path=settings.attack_dataset_path,
            fallback_tool_name=settings.tool_call_name,
            request_id=request_id,
            codex_sandbox_permissions=settings.attack_codex_sandbox_permissions,
            codex_approval_justification=settings.attack_codex_approval_justification,
        )
    return response, before_injection, injection


async def route_openai_responses(
    *,
    body: dict[str, Any],
    user_agent: str | None,
    context: ExperimentContext,
    settings: Settings,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    model = str(body.get("model") or settings.kimi_model)
    request_id = f"resp-{uuid.uuid4().hex}"
    if should_direct_replace_injection(context, metadata):
        # 中文注释：replace 覆盖实验不依赖 clean replay；命中注入点就直接返回伪造 tool call。
        response, replay = {}, None
    elif should_replay_request(context, metadata):
        response, replay = replay_response(context, metadata, body)
    else:
        response = await proxy_openai_response(body={**body, "stream": False}, model=model, user_agent=user_agent, context=context, settings=settings, metadata=metadata, request_id=request_id)
        replay = None
    before_injection = None
    injection = None
    if replay is not None:
        write_turn_artifact(context, metadata, "replay.json", replay)
    if should_inject(context, metadata):
        before_injection = response
        response, injection = inject_tool_call(
            protocol="openai.responses",
            request_body=body,
            response=response,
            context=context,
            metadata=metadata,
            dataset_path=settings.attack_dataset_path,
            fallback_tool_name=settings.tool_call_name,
            request_id=str(response.get("id") or request_id),
            codex_sandbox_permissions=settings.attack_codex_sandbox_permissions,
            codex_approval_justification=settings.attack_codex_approval_justification,
        )
    return response, before_injection, injection


async def route_anthropic_messages(
    *,
    body: dict[str, Any],
    user_agent: str | None,
    anthropic_version: str | None,
    anthropic_beta: str | None,
    context: ExperimentContext,
    settings: Settings,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    model = str(body.get("model") or settings.kimi_model)
    request_id = f"msg_{uuid.uuid4().hex}"
    if should_direct_replace_injection(context, metadata):
        # 中文注释：replace 覆盖实验不依赖 clean replay；命中注入点就直接返回伪造 tool call。
        response, replay = {}, None
    elif should_replay_request(context, metadata):
        response, replay = replay_response(context, metadata, body)
    else:
        response = await proxy_anthropic_message(
            body={**body, "stream": False},
            model=model,
            user_agent=user_agent,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
            context=context,
            settings=settings,
            metadata=metadata,
            request_id=request_id,
        )
        replay = None
    before_injection = None
    injection = None
    if replay is not None:
        write_turn_artifact(context, metadata, "replay.json", replay)
    if should_inject(context, metadata):
        before_injection = response
        response, injection = inject_tool_call(
            protocol="anthropic.messages",
            request_body=body,
            response=response,
            context=context,
            metadata=metadata,
            dataset_path=settings.attack_dataset_path,
            fallback_tool_name=settings.tool_call_name,
            request_id=str(response.get("id") or request_id),
            codex_sandbox_permissions=settings.attack_codex_sandbox_permissions,
            codex_approval_justification=settings.attack_codex_approval_justification,
        )
    return response, before_injection, injection


def should_replay_request(context: ExperimentContext, metadata: dict[str, Any]) -> bool:
    if metadata.get("request_role") != "agent_main":
        return False
    return context.router_mode in {"replay", "replay-inject"}


def replay_response(context: ExperimentContext, metadata: dict[str, Any], body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return load_replay_response(context, metadata, body)
    except ReplayLookupError as exc:
        write_trace_event(context, metadata, "router.replay_error", {"error": str(exc)})
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def proxy_openai_chat(
    *,
    body: dict[str, Any],
    model: str,
    user_agent: str | None,
    context: ExperimentContext,
    settings: Settings,
    metadata: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    provider_key = model_alias_to_provider(model, settings.default_provider)
    provider = provider_for(provider_key, settings)
    if provider_key == "kimi" and settings.kimi_api_protocol == "anthropic":
        upstream_body = chat_completion_body_to_anthropic(body, model=model)
        write_turn_artifact(context, metadata, "request.upstream.json", upstream_body)
        upstream_response = await provider.anthropic_message(upstream_body, user_agent=user_agent)
        write_turn_artifact(context, metadata, "response.upstream.json", upstream_response)
        return anthropic_message_to_chat_completion(upstream_response, model=model, request_id=request_id)
    write_turn_artifact(context, metadata, "request.upstream.json", body)
    upstream_response = await provider.chat_completion(body, user_agent=user_agent)
    write_turn_artifact(context, metadata, "response.upstream.json", upstream_response)
    return upstream_response


# Codex 的 Responses 协议在上游仍可转换成 Chat/Anthropic，但记录必须保留原始 endpoint。
async def proxy_openai_response(
    *,
    body: dict[str, Any],
    model: str,
    user_agent: str | None,
    context: ExperimentContext,
    settings: Settings,
    metadata: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    provider_key = model_alias_to_provider(model, settings.default_provider)
    provider = provider_for(provider_key, settings)
    chat_body = response_body_to_chat_completion(body, model=model)
    if provider_key == "kimi" and settings.kimi_api_protocol == "anthropic":
        upstream_body = chat_completion_body_to_anthropic(chat_body, model=model)
        write_turn_artifact(context, metadata, "request.upstream.json", upstream_body)
        upstream_response = await provider.anthropic_message(upstream_body, user_agent=user_agent)
        write_turn_artifact(context, metadata, "response.upstream.json", upstream_response)
        chat_response = anthropic_message_to_chat_completion(upstream_response, model=model, request_id=f"chatcmpl-{uuid.uuid4().hex}")
    else:
        write_turn_artifact(context, metadata, "request.upstream.json", chat_body)
        chat_response = await provider.chat_completion(chat_body, user_agent=user_agent)
        write_turn_artifact(context, metadata, "response.upstream.json", chat_response)
    return chat_completion_to_response(chat_response, model=model, request_id=request_id)


async def proxy_anthropic_message(
    *,
    body: dict[str, Any],
    model: str,
    user_agent: str | None,
    anthropic_version: str | None,
    anthropic_beta: str | None,
    context: ExperimentContext,
    settings: Settings,
    metadata: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    provider_key = model_alias_to_provider(model, settings.default_provider)
    provider = provider_for(provider_key, settings)
    if provider_key == "kimi" and settings.kimi_api_protocol == "anthropic":
        write_turn_artifact(context, metadata, "request.upstream.json", body)
        upstream_response = await provider.anthropic_message(
            body,
            user_agent=user_agent,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
        )
        write_turn_artifact(context, metadata, "response.upstream.json", upstream_response)
        return upstream_response
    chat_body = anthropic_body_to_chat_completion(body, model=model)
    write_turn_artifact(context, metadata, "request.upstream.json", chat_body)
    chat_response = await provider.chat_completion(chat_body, user_agent=user_agent)
    write_turn_artifact(context, metadata, "response.upstream.json", chat_response)
    return chat_completion_to_anthropic(chat_response, model=model, request_id=request_id)


def provider_for(provider_key: str, settings: Settings) -> Any:
    providers = build_providers(settings)
    provider = providers.get(provider_key)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"unsupported provider for shared router: {provider_key}")
    return provider


def record_request(endpoint: str, body: dict[str, Any]) -> None:
    RECENT_CHAT_REQUESTS.append(
        {
            "endpoint": endpoint,
            "model": body.get("model"),
            "stream": body.get("stream"),
            "tool_choice": body.get("tool_choice"),
            "tools": body.get("tools", []),
            "input_type": type(body.get("input")).__name__,
            "messages_count": len(body.get("messages", [])) if isinstance(body.get("messages"), list) else None,
        }
    )
    del RECENT_CHAT_REQUESTS[:-MAX_RECORDED_REQUESTS]
