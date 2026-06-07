from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import HTTPException


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def prepare_body(self, body: dict[str, Any]) -> dict[str, Any]:
        forwarded = dict(body)
        forwarded["model"] = self.model
        return forwarded

    def headers(self) -> dict[str, str]:
        if not self.api_key:
            raise HTTPException(
                status_code=500,
                detail=f"{self.name} API key is not configured",
            )
        return {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

    async def chat_completion(self, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers(),
                json=self.prepare_body(body),
            )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json()

    async def stream_chat_completion(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self.headers(),
                json=self.prepare_body({**body, "stream": True}),
            ) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=error_body.decode("utf-8", errors="replace"),
                    )
                async for chunk in response.aiter_bytes():
                    yield chunk
