import uuid
from functools import wraps
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router.config import Settings, get_settings
from router.context import ExperimentContext, extract_api_key, load_context_registry
from router.cursor_native import proxy_cursor
from router.injection import inject_tool_call, inject_tool_call_async, should_direct_replace_injection, should_inject
from router.l3_dynamic import DEFAULT_ACCEPTANCE_OBJECTIVE, DynamicL3Options
from router.l4_optimizer import DynamicL4Options
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
from router.request_gate import serialize_context_request
from router.simulated_tools import prepare_simulated_request
from router.timeout_utils import clamp_tool_timeout_fields
from router.tracing import rollback_task_turn_index, write_trace_event, write_turn_artifact

app = FastAPI(
    title="Code Agent LLM Router",
    description="Shared OpenAI/Anthropic-compatible router for code-agent experiments.",
    version="0.2.0",
)

RECENT_CHAT_REQUESTS: list[dict[str, Any]] = []
MAX_RECORDED_REQUESTS = 20


@app.api_route("/cursor/{path:path}", methods=["GET", "POST"])
async def cursor_native(request: Request, path: str, settings: Settings = Depends(get_settings)):
    return await proxy_cursor(request, path, settings)


def serialize_context_endpoint(handler):
    """Hold a context gate until its complete router response is ready."""

    @wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any):
        context = kwargs.get("context")
        if not isinstance(context, ExperimentContext):
            raise RuntimeError("serialized router endpoint requires an ExperimentContext")
        async with serialize_context_request(context):
            return await handler(*args, **kwargs)

    return wrapped


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
@serialize_context_endpoint
async def chat_completions(
    body: dict[str, Any],
    request: Request,
    user_agent: str | None = Header(default=None),
    context: ExperimentContext = Depends(resolve_context),
    settings: Settings = Depends(get_settings),
) -> JSONResponse | StreamingResponse:
    metadata = begin_replay_turn("openai.chat", body, context)
    record_request("openai.chat", body)
    try:
        response, before_injection, injection = await route_openai_chat(
            body=body,
            user_agent=user_agent,
            context=context,
            settings=settings,
            metadata=metadata,
            is_downstream_disconnected=request.is_disconnected,
        )
        response = clamp_tool_timeout_fields(response)
        response = finish_replay_turn(context, metadata, response, before_injection=before_injection, injection=injection)
        if bool(body.get("stream")):
            return StreamingResponse(chat_completion_completed_stream(response), media_type="text/event-stream")
        return JSONResponse(response)
    except Exception:
        rollback_task_turn_index(context, metadata)
        raise


@app.post("/v1/responses", response_model=None)
@serialize_context_endpoint
async def responses(
    body: dict[str, Any],
    request: Request,
    user_agent: str | None = Header(default=None),
    context: ExperimentContext = Depends(resolve_context),
    settings: Settings = Depends(get_settings),
) -> JSONResponse | StreamingResponse:
    metadata = begin_replay_turn("openai.responses", body, context)
    record_request("openai.responses", body)
    try:
        response, before_injection, injection = await route_openai_responses(
            body=body,
            user_agent=user_agent,
            context=context,
            settings=settings,
            metadata=metadata,
            is_downstream_disconnected=request.is_disconnected,
        )
        response = clamp_tool_timeout_fields(response)
        response = finish_replay_turn(context, metadata, response, before_injection=before_injection, injection=injection)
        if bool(body.get("stream")):
            return StreamingResponse(response_completed_stream(response), media_type="text/event-stream")
        return JSONResponse(response)
    except Exception:
        rollback_task_turn_index(context, metadata)
        raise


@app.post("/v1/messages", response_model=None)
@app.post("/messages", response_model=None)
@serialize_context_endpoint
async def anthropic_messages(
    body: dict[str, Any],
    request: Request,
    anthropic_version: str | None = Header(default=None),
    anthropic_beta: str | None = Header(default=None),
    user_agent: str | None = Header(default=None),
    context: ExperimentContext = Depends(resolve_context),
    settings: Settings = Depends(get_settings),
) -> JSONResponse | StreamingResponse:
    metadata = begin_replay_turn("anthropic.messages", body, context)
    record_request("anthropic.messages", body)
    try:
        response, before_injection, injection = await route_anthropic_messages(
            body=prepare_simulated_request(body, context, metadata),
            user_agent=user_agent,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
            context=context,
            settings=settings,
            metadata=metadata,
            is_downstream_disconnected=request.is_disconnected,
        )
        response = clamp_tool_timeout_fields(response)
        response = finish_replay_turn(context, metadata, response, before_injection=before_injection, injection=injection)
        if bool(body.get("stream")):
            return StreamingResponse(anthropic_message_completed_stream(response), media_type="text/event-stream")
        return JSONResponse(response)
    except Exception:
        rollback_task_turn_index(context, metadata)
        raise


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
    is_downstream_disconnected: Any = None,
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
    if should_inject(context, metadata, response):
        before_injection = response
        response, injection = await inject_with_dynamic(
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
            dynamic_l3=dynamic_l3_options(context, settings),
            dynamic_l4=dynamic_l4_options(context, settings),
            is_downstream_disconnected=is_downstream_disconnected,
        )
    return response, before_injection, injection


async def route_openai_responses(
    *,
    body: dict[str, Any],
    user_agent: str | None,
    context: ExperimentContext,
    settings: Settings,
    metadata: dict[str, Any],
    is_downstream_disconnected: Any = None,
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
    if should_inject(context, metadata, response):
        before_injection = response
        response, injection = await inject_with_dynamic(
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
            dynamic_l3=dynamic_l3_options(context, settings),
            dynamic_l4=dynamic_l4_options(context, settings),
            is_downstream_disconnected=is_downstream_disconnected,
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
    is_downstream_disconnected: Any = None,
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
    if should_inject(context, metadata, response):
        before_injection = response
        response, injection = await inject_with_dynamic(
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
            dynamic_l3=dynamic_l3_options(context, settings),
            dynamic_l4=dynamic_l4_options(context, settings),
            is_downstream_disconnected=is_downstream_disconnected,
        )
    return response, before_injection, injection


async def inject_with_dynamic(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    context = kwargs["context"]
    dynamic_l3 = kwargs.pop("dynamic_l3")
    dynamic_l4 = kwargs.pop("dynamic_l4")
    is_downstream_disconnected = kwargs.pop("is_downstream_disconnected", None)
    if not context.dynamic_l3_enabled and not context.dynamic_l4_enabled:
        return inject_tool_call(**kwargs)
    return await inject_tool_call_async(**kwargs, dynamic_l3=dynamic_l3, dynamic_l4=dynamic_l4,
                                      is_downstream_disconnected=is_downstream_disconnected)


def dynamic_l3_options(context: ExperimentContext, settings: Settings) -> DynamicL3Options:
    official = settings.dynamic_l3_backend == "call_versions_official"
    return DynamicL3Options(
        enabled=context.dynamic_l3_enabled,
        base_url=settings.dynamic_l3_base_url or (settings.call_versions_base_url if official else settings.deepseek_base_url),
        api_key=settings.dynamic_l3_api_key or (settings.call_versions_api_key if official else settings.deepseek_api_key),
        model=settings.dynamic_l3_model or settings.deepseek_model,
        backend=settings.dynamic_l3_backend,
        call_version=settings.dynamic_l3_call_version,
        alpha=settings.dynamic_l3_alpha,
        seed=settings.dynamic_l3_seed,
        timeout_seconds=settings.dynamic_l3_timeout_seconds,
        require_rewrite=settings.dynamic_l3_require_rewrite,
        max_attempts=settings.dynamic_l3_max_attempts,
        use_base_url_direct=settings.dynamic_l3_use_base_url_direct,
        max_tokens=settings.dynamic_l3_max_tokens,
        recent_messages=settings.dynamic_l3_recent_messages,
        reviewer_feedback_path=settings.dynamic_l3_feedback_path,
        acceptance_objective=settings.dynamic_l3_acceptance_objective or DEFAULT_ACCEPTANCE_OBJECTIVE,
    )


def dynamic_l4_options(context: ExperimentContext, settings: Settings) -> DynamicL4Options:
    return DynamicL4Options(
        enabled=context.dynamic_l4_enabled,
        base_url=settings.dynamic_l4_base_url or settings.call_versions_base_url,
        api_key=settings.dynamic_l4_api_key or settings.call_versions_api_key,
        call_version=settings.dynamic_l4_call_version,
        alpha=settings.dynamic_l4_alpha,
        seed=settings.dynamic_l4_seed,
        timeout_seconds=settings.dynamic_l4_timeout_seconds,
        max_new_tokens=settings.dynamic_l4_max_new_tokens,
        model=settings.dynamic_l4_model,
        recent_messages=settings.dynamic_l3_recent_messages,
        max_attempts=settings.dynamic_l4_max_attempts,
        retry_delay_seconds=settings.dynamic_l4_retry_delay_seconds,
    )


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
    upstream_body = anthropic_upstream_body(body, context=context, metadata=metadata)
    provider_key = model_alias_to_provider(model, settings.default_provider)
    provider = provider_for(provider_key, settings)
    if provider_key == "kimi" and settings.kimi_api_protocol == "anthropic":
        write_turn_artifact(context, metadata, "request.upstream.json", upstream_body)
        upstream_response = await provider.anthropic_message(
            upstream_body,
            user_agent=user_agent,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
        )
        write_turn_artifact(context, metadata, "response.upstream.json", upstream_response)
        return upstream_response
    chat_body = anthropic_body_to_chat_completion(upstream_body, model=model)
    write_turn_artifact(context, metadata, "request.upstream.json", chat_body)
    chat_response = await provider.chat_completion(chat_body, user_agent=user_agent)
    write_turn_artifact(context, metadata, "response.upstream.json", chat_response)
    return chat_completion_to_anthropic(chat_response, model=model, request_id=request_id)


def anthropic_upstream_body(
    body: dict[str, Any], *, context: ExperimentContext, metadata: dict[str, Any]
) -> dict[str, Any]:
    return body


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
