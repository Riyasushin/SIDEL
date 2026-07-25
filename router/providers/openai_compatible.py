import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import re
import time

import httpx
from fastapi import HTTPException


_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{12,}")
_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "x_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "kimi_api_key",
    "cookie",
    "set_cookie",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower().replace("-", "_") in _SECRET_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_PATTERN.sub("sk-<redacted>", value)
    return value


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_attempts: int = 0,
        anthropic_base_url: str = "",
        trace_dir: str = "",
        trace_run_id: str = "",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.anthropic_base_url = anthropic_base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.trace_run_id = trace_run_id

    def _should_retry_status(self, status_code: int) -> bool:
        return status_code in {429, 500, 502, 503, 504}

    def _can_retry(self, attempt: int) -> bool:
        return self.max_attempts <= 0 or attempt < self.max_attempts

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return max(0.0, min(float(retry_after), 60.0))
                except ValueError:
                    pass
        return min(float(2 ** min(attempt - 1, 6)), 60.0)

    async def _post_json_with_retry(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        request_event: str,
        response_event: str,
        error_event: str,
        exception_event: str,
        path: str,
    ) -> dict[str, Any]:
        max_attempts = self.max_attempts
        last_exception: httpx.RequestError | None = None
        last_retryable_status: tuple[int, str] | None = None
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            attempt = 0
            while True:
                attempt += 1
                can_retry = self._can_retry(attempt)
                started = time.monotonic()
                self.trace(
                    request_event,
                    {
                        "path": path,
                        "headers": headers,
                        "body": payload,
                        "attempt": attempt,
                        "max_attempts": max_attempts or "unlimited",
                    },
                )
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except httpx.RequestError as exc:
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    self.trace(
                        exception_event,
                        {
                            "path": path,
                            "elapsed_ms": elapsed_ms,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "attempt": attempt,
                            "max_attempts": max_attempts or "unlimited",
                            "will_retry": can_retry,
                        },
                    )
                    last_exception = exc
                    if can_retry:
                        await asyncio.sleep(self._retry_delay(None, attempt))
                        continue
                    raise HTTPException(
                        status_code=502,
                        detail=f"{self.name} upstream request failed after {attempt} attempts: {type(exc).__name__}: {exc}",
                    ) from exc

                elapsed_ms = int((time.monotonic() - started) * 1000)
                if response.status_code >= 400:
                    retryable = self._should_retry_status(response.status_code)
                    self.trace(
                        error_event,
                        {
                            "path": path,
                            "status_code": response.status_code,
                            "elapsed_ms": elapsed_ms,
                            "body": response.text,
                            "attempt": attempt,
                            "max_attempts": max_attempts or "unlimited",
                            "will_retry": retryable and can_retry,
                        },
                    )
                    if retryable and can_retry:
                        last_retryable_status = (response.status_code, response.text)
                        await asyncio.sleep(self._retry_delay(response, attempt))
                        continue
                    raise HTTPException(status_code=response.status_code, detail=response.text)

                body = response.json()
                self.trace(
                    response_event,
                    {
                        "path": path,
                        "status_code": response.status_code,
                        "elapsed_ms": elapsed_ms,
                        "body": body,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                    },
                )
                return body

        if last_retryable_status is not None:
            raise HTTPException(status_code=last_retryable_status[0], detail=last_retryable_status[1])
        if last_exception is not None:
            raise HTTPException(
                status_code=502,
                detail=f"{self.name} upstream request failed after {max_attempts} attempts: {type(last_exception).__name__}: {last_exception}",
            ) from last_exception
        raise HTTPException(status_code=502, detail=f"{self.name} upstream request failed without a response")

    def prepare_body(self, body: dict[str, Any]) -> dict[str, Any]:
        forwarded = dict(body)
        forwarded["model"] = self.model
        return forwarded

    def openai_headers(self, *, user_agent: str | None = None) -> dict[str, str]:
        if not self.api_key:
            raise HTTPException(
                status_code=500,
                detail=f"{self.name} API key is not configured",
            )
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }
        if user_agent:
            headers["user-agent"] = user_agent
        return headers

    def anthropic_headers(
        self,
        *,
        user_agent: str | None = None,
        anthropic_version: str | None = None,
        anthropic_beta: str | None = None,
    ) -> dict[str, str]:
        if not self.api_key:
            raise HTTPException(
                status_code=500,
                detail=f"{self.name} API key is not configured",
            )
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": anthropic_version or "2023-06-01",
            "content-type": "application/json",
        }
        if anthropic_beta:
            headers["anthropic-beta"] = anthropic_beta
        if user_agent:
            headers["user-agent"] = user_agent
        return headers

    def trace(self, event: str, payload: dict[str, Any]) -> None:
        if self.trace_dir is None:
            return
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.trace_run_id,
            "provider": self.name,
            "event": event,
            **_redact(payload),
        }
        path = self.trace_dir / f"router-{self.name}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def chat_completion(self, body: dict[str, Any], *, user_agent: str | None = None) -> dict[str, Any]:
        prepared = self.prepare_body(body)
        headers = self.openai_headers(user_agent=user_agent)
        return await self._post_json_with_retry(
            url=f"{self.base_url}/chat/completions",
            headers=headers,
            payload=prepared,
            request_event="upstream.request",
            response_event="upstream.response",
            error_event="upstream.error",
            exception_event="upstream.exception",
            path="/chat/completions",
        )

    async def stream_chat_completion(self, body: dict[str, Any], *, user_agent: str | None = None) -> AsyncIterator[bytes]:
        prepared = self.prepare_body({**body, "stream": True})
        started = time.monotonic()
        headers = self.openai_headers(user_agent=user_agent)
        self.trace("upstream.stream_request", {"path": "/chat/completions", "headers": headers, "body": prepared})
        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=prepared,
            ) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    text = error_body.decode("utf-8", errors="replace")
                    self.trace(
                        "upstream.stream_error",
                        {"path": "/chat/completions", "status_code": response.status_code, "elapsed_ms": elapsed_ms, "body": text},
                    )
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=text,
                    )
                index = 0
                async for chunk in response.aiter_bytes():
                    self.trace(
                        "upstream.stream_chunk",
                        {
                            "path": "/chat/completions",
                            "status_code": response.status_code,
                            "chunk_index": index,
                            "body": chunk.decode("utf-8", errors="replace"),
                        },
                    )
                    index += 1
                    yield chunk
                elapsed_ms = int((time.monotonic() - started) * 1000)
                self.trace(
                    "upstream.stream_done",
                    {"path": "/chat/completions", "status_code": response.status_code, "elapsed_ms": elapsed_ms, "chunks": index},
                )

    def prepare_anthropic_body(self, body: dict[str, Any]) -> dict[str, Any]:
        forwarded = dict(body)
        forwarded["model"] = self.model
        return forwarded

    def anthropic_endpoint(self) -> str:
        if not self.anthropic_base_url:
            raise HTTPException(status_code=400, detail=f"{self.name} does not support Anthropic-compatible upstream calls")
        return f"{self.anthropic_base_url}/v1/messages"

    async def anthropic_message(
        self,
        body: dict[str, Any],
        *,
        user_agent: str | None = None,
        anthropic_version: str | None = None,
        anthropic_beta: str | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_anthropic_body(body)
        headers = self.anthropic_headers(
            user_agent=user_agent,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
        )
        return await self._post_json_with_retry(
            url=self.anthropic_endpoint(),
            headers=headers,
            payload=prepared,
            request_event="upstream.anthropic_request",
            response_event="upstream.anthropic_response",
            error_event="upstream.anthropic_error",
            exception_event="upstream.anthropic_exception",
            path="/v1/messages",
        )

    async def stream_anthropic_message(
        self,
        body: dict[str, Any],
        *,
        user_agent: str | None = None,
        anthropic_version: str | None = None,
        anthropic_beta: str | None = None,
    ) -> AsyncIterator[bytes]:
        prepared = self.prepare_anthropic_body({**body, "stream": True})
        started = time.monotonic()
        headers = self.anthropic_headers(
            user_agent=user_agent,
            anthropic_version=anthropic_version,
            anthropic_beta=anthropic_beta,
        )
        self.trace("upstream.anthropic_stream_request", {"path": "/v1/messages", "headers": headers, "body": prepared})
        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            async with client.stream(
                "POST",
                self.anthropic_endpoint(),
                headers=headers,
                json=prepared,
            ) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    text = error_body.decode("utf-8", errors="replace")
                    self.trace(
                        "upstream.anthropic_stream_error",
                        {"path": "/v1/messages", "status_code": response.status_code, "elapsed_ms": elapsed_ms, "body": text},
                    )
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=text,
                    )
                index = 0
                async for chunk in response.aiter_bytes():
                    self.trace(
                        "upstream.anthropic_stream_chunk",
                        {
                            "path": "/v1/messages",
                            "status_code": response.status_code,
                            "chunk_index": index,
                            "body": chunk.decode("utf-8", errors="replace"),
                        },
                    )
                    index += 1
                    yield chunk
                elapsed_ms = int((time.monotonic() - started) * 1000)
                self.trace(
                    "upstream.anthropic_stream_done",
                    {"path": "/v1/messages", "status_code": response.status_code, "elapsed_ms": elapsed_ms, "chunks": index},
                )
