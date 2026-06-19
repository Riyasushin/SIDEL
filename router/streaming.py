"""SSE response renderers for non-streaming router results.

上游 replay/inject 路径会先得到完整响应，再用这些 helper 伪装成客户端期望的
streaming event 形状。这里不做实验决策。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


async def chat_completion_completed_stream(chat_response: dict[str, Any]) -> AsyncIterator[bytes]:
    yield sse(chat_response)
    yield b"data: [DONE]\n\n"

async def response_completed_stream(response: dict[str, Any]) -> AsyncIterator[bytes]:
    yield event_sse("response.created", {"type": "response.created", "response": {**response, "output": []}})
    for index, item in enumerate(response.get("output", [])):
        yield event_sse(
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": index, "item": item},
        )
        yield event_sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": index, "item": item},
        )
    yield event_sse("response.completed", {"type": "response.completed", "response": response})
    yield b"data: [DONE]\n\n"

async def anthropic_message_completed_stream(response: dict[str, Any]) -> AsyncIterator[bytes]:
    message = {**response, "content": []}
    yield event_sse("message_start", {"type": "message_start", "message": message})
    for index, block in enumerate(response.get("content", [])):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            yield event_sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            text = str(block.get("text") or "")
            if text:
                yield event_sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}})
            yield event_sse("content_block_stop", {"type": "content_block_stop", "index": index})
        elif block.get("type") == "tool_use":
            partial = {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": {}}
            yield event_sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": partial})
            input_json = json.dumps(block.get("input") or {}, ensure_ascii=False)
            yield event_sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": input_json}})
            yield event_sse("content_block_stop", {"type": "content_block_stop", "index": index})
    yield event_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": response.get("stop_reason") or "end_turn", "stop_sequence": None}, "usage": response.get("usage") or {"output_tokens": 0}})
    yield event_sse("message_stop", {"type": "message_stop"})

def event_sse(event: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")

def sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
