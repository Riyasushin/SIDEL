from typing import Any


OPENAI_CHAT_COMPLETIONS_PATH = "/chat/completions"
OPENAI_RESPONSES_PATH = "/responses"


def current_timestamp() -> int:
    import time

    return int(time.time())


def model_alias_to_provider(model: str, default_provider: str) -> str:
    normalized = model.lower()
    if "deepseek" in normalized:
        return "deepseek"
    if "kimi" in normalized or "moonshot" in normalized:
        return "kimi"
    return default_provider


def normalize_chat_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if isinstance(messages, list):
        return messages

    input_value = body.get("input")
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if isinstance(input_value, list):
        return input_value
    return [{"role": "user", "content": ""}]


def zero_chat_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def zero_response_usage() -> dict[str, int]:
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def build_chat_completion(
    *,
    model: str,
    content: str,
    request_id: str,
    created: int | None = None,
) -> dict[str, Any]:
    created_at = created or current_timestamp()
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": created_at,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": zero_chat_usage(),
    }


def build_tool_call_completion(
    *,
    model: str,
    tool_name: str,
    arguments: str,
    request_id: str,
    content: str | None = None,
    created: int | None = None,
) -> dict[str, Any]:
    created_at = created or current_timestamp()
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": created_at,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": f"call_{request_id[-12:]}",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": zero_chat_usage(),
    }


def build_chat_completion_chunk(
    *,
    model: str,
    content: str,
    request_id: str,
    created: int | None = None,
    done: bool = False,
) -> dict[str, Any]:
    created_at = created or current_timestamp()
    delta: dict[str, Any] = {} if done else {"role": "assistant", "content": content}
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created_at,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if done else None}],
    }


def build_tool_call_chunk(
    *,
    model: str,
    tool_name: str,
    arguments: str,
    request_id: str,
    created: int | None = None,
    done: bool = False,
) -> dict[str, Any]:
    created_at = created or current_timestamp()
    delta: dict[str, Any] = {}
    finish_reason = "tool_calls" if done else None
    if not done:
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"call_{request_id[-12:]}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": arguments},
                }
            ]
        }
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created_at,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def build_response_object(
    *,
    model: str,
    content: str,
    request_id: str,
    created: int | None = None,
) -> dict[str, Any]:
    created_at = created or current_timestamp()
    return {
        "id": request_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": f"{request_id}-msg",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        ],
        "usage": zero_response_usage(),
    }


def build_response_tool_call(
    *,
    model: str,
    tool_name: str,
    arguments: str,
    request_id: str,
    created: int | None = None,
) -> dict[str, Any]:
    created_at = created or current_timestamp()
    return {
        "id": request_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": f"fc_{request_id[-12:]}",
                "type": "function_call",
                "status": "completed",
                "call_id": f"call_{request_id[-12:]}",
                "name": tool_name,
                "arguments": arguments,
            }
        ],
        "parallel_tool_calls": False,
        "usage": zero_response_usage(),
    }


def build_anthropic_message(
    *,
    model: str,
    content: str,
    request_id: str,
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def build_anthropic_tool_use(
    *,
    model: str,
    tool_name: str,
    tool_input: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {
                "type": "tool_use",
                "id": f"toolu_attack_{request_id[-12:]}",
                "name": tool_name,
                "input": tool_input,
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
