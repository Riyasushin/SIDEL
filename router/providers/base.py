from collections.abc import AsyncIterator
from typing import Any, Protocol


class ChatProvider(Protocol):
    async def chat_completion(self, body: dict[str, Any]) -> dict[str, Any]:
        ...

    async def stream_chat_completion(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        ...
