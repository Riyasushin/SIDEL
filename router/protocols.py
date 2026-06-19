"""Protocol conversion helpers for router upstream/downstream adapters.

这些函数只处理协议结构转换，不读取 settings，也不写 trace。这样 FastAPI
handler、provider proxy 和实验注入逻辑不会互相污染。
"""

from __future__ import annotations

import json
from typing import Any

from router.models import build_response_object, current_timestamp, normalize_chat_messages


def response_body_to_chat_completion(body: dict[str, Any], *, model: str) -> dict[str, Any]:
    chat_body: dict[str, Any] = {
        "model": model,
        "messages": response_input_to_chat_messages(body.get("input")),
        "stream": False,
    }
    max_tokens = body.get("max_output_tokens") or body.get("max_tokens")
    if max_tokens:
        chat_body["max_tokens"] = max_tokens
    if "temperature" in body:
        chat_body["temperature"] = body["temperature"]
    tools = response_tools_to_chat_tools(body.get("tools"))
    if tools:
        chat_body["tools"] = tools
        if body.get("tool_choice"):
            chat_body["tool_choice"] = body["tool_choice"]
    return chat_body

def response_input_to_chat_messages(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if not isinstance(input_value, list):
        return [{"role": "user", "content": ""}]

    messages: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []

    def flush_pending_tool_calls() -> None:
        if not pending_tool_calls:
            return
        messages.append({"role": "assistant", "content": "", "tool_calls": list(pending_tool_calls)})
        pending_tool_calls.clear()

    for item in input_value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"function_call", "tool_call"}:
            pending_tool_calls.append(response_function_call_to_chat_tool_call(item))
            continue
        if item_type in {"function_call_output", "tool_result"}:
            flush_pending_tool_calls()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("tool_call_id") or "call_unknown",
                    "content": coerce_text(item.get("output") or item.get("content")),
                }
            )
            continue
        if item_type in {"message", "input_message"} or "role" in item:
            flush_pending_tool_calls()
            role = item.get("role") if item.get("role") in {"system", "user", "assistant", "tool"} else "user"
            messages.append({"role": role, "content": response_content_to_text(item.get("content"))})
    flush_pending_tool_calls()
    return messages or [{"role": "user", "content": ""}]

def response_function_call_to_chat_tool_call(item: dict[str, Any]) -> dict[str, Any]:
    call_id = str(item.get("call_id") or item.get("id") or "call_unknown")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": str(item.get("name") or "tool"),
            "arguments": coerce_json_arguments(item.get("arguments")),
        },
    }

def response_function_call_to_chat_message(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": item.get("content") or "",
        "tool_calls": [response_function_call_to_chat_tool_call(item)],
    }

def response_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(coerce_text(part.get("text") or part.get("content") or part.get("output")))
        return "\n".join(part for part in parts if part)
    return coerce_text(content)

def response_tools_to_chat_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    chat_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            chat_tools.append(tool)
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        chat_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description") or "",
                    "parameters": tool.get("parameters") or {},
                },
            }
        )
    return chat_tools

def chat_completion_body_to_anthropic(body: dict[str, Any], *, model: str) -> dict[str, Any]:
    anthropic_body: dict[str, Any] = {
        "model": model,
        "max_tokens": int(body.get("max_tokens") or body.get("max_completion_tokens") or 4096),
        "messages": chat_messages_to_anthropic_messages(body),
    }
    system = chat_messages_to_anthropic_system(body)
    if system:
        anthropic_body["system"] = system
    if "temperature" in body:
        anthropic_body["temperature"] = body["temperature"]
    tools = chat_tools_to_anthropic_tools(body.get("tools"))
    if tools:
        anthropic_body["tools"] = tools
    return anthropic_body

def chat_messages_to_anthropic_system(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for message in normalize_chat_messages(body):
        if isinstance(message, dict) and message.get("role") == "system":
            text = response_content_to_text(message.get("content"))
            if text:
                parts.append(text)
    return "\n\n".join(parts)

def chat_messages_to_anthropic_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for message in normalize_chat_messages(body):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            continue
        if role == "tool" or message.get("tool_call_id"):
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or "toolu_unknown"),
                            "content": response_content_to_text(message.get("content")),
                        }
                    ],
                }
            )
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        content_blocks: list[dict[str, Any]] = []
        text = response_content_to_text(message.get("content"))
        if text:
            content_blocks.append({"type": "text", "text": text})
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list):
            for index, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or f"toolu_unknown_{index}"),
                        "name": str(function.get("name") or tool_call.get("name") or "tool"),
                        "input": parse_json_object(function.get("arguments") or tool_call.get("arguments")),
                    }
                )
        if not content_blocks:
            content_blocks.append({"type": "text", "text": ""})
        messages.append({"role": role, "content": content_blocks})
    return messages or [{"role": "user", "content": [{"type": "text", "text": ""}]}]

def chat_tools_to_anthropic_tools(tools: Any) -> list[dict[str, Any]]:
    chat_tools = response_tools_to_chat_tools(tools)
    anthropic_tools: list[dict[str, Any]] = []
    for tool in chat_tools:
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = function.get("name")
        if not isinstance(name, str):
            continue
        anthropic_tools.append(
            {
                "name": name,
                "description": function.get("description") or "",
                "input_schema": function.get("parameters") or {},
            }
        )
    return anthropic_tools

def anthropic_message_to_chat_completion(anthropic_response: dict[str, Any], *, model: str, request_id: str) -> dict[str, Any]:
    content_blocks = anthropic_response.get("content")
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    if isinstance(content_blocks, list):
        for index, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = coerce_text(block.get("text"))
                if text:
                    text_parts.append(text)
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or f"toolu_{request_id[-12:]}_{index}"),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or "tool"),
                            "arguments": coerce_json_arguments(block.get("input") or {}),
                        },
                    }
                )
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": current_timestamp(),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": anthropic_usage_to_chat_usage(anthropic_response.get("usage")),
    }

def anthropic_usage_to_chat_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }

def chat_completion_to_response(chat_response: dict[str, Any], *, model: str, request_id: str) -> dict[str, Any]:
    choice = first_choice(chat_response)
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        output: list[dict[str, Any]] = []
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            output.append(
                {
                    "id": f"fc_{request_id[-12:]}_{index}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": str(tool_call.get("id") or f"call_{request_id[-12:]}_{index}"),
                    "name": str(function.get("name") or tool_call.get("name") or "tool"),
                    "arguments": coerce_json_arguments(function.get("arguments") or tool_call.get("arguments")),
                }
            )
        if output:
            return {
                "id": request_id,
                "object": "response",
                "created_at": current_timestamp(),
                "status": "completed",
                "model": model,
                "output": output,
                "parallel_tool_calls": len(output) > 1,
                "usage": chat_usage_to_response_usage(chat_response.get("usage")),
            }
    response = build_response_object(
        model=model,
        content=coerce_text(message.get("content")),
        request_id=request_id,
    )
    response["usage"] = chat_usage_to_response_usage(chat_response.get("usage"))
    return response

def anthropic_body_to_chat_completion(body: dict[str, Any], *, model: str) -> dict[str, Any]:
    chat_body: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages_to_chat(body),
        "stream": False,
    }
    if body.get("max_tokens"):
        chat_body["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        chat_body["temperature"] = body["temperature"]
    tools = anthropic_tools_to_chat_tools(body.get("tools"))
    if tools:
        chat_body["tools"] = tools
    return chat_body

def anthropic_messages_to_chat(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": response_content_to_text(system)})
    for message in normalize_chat_messages(body):
        if not isinstance(message, dict):
            continue
        role = message.get("role") if message.get("role") in {"user", "assistant"} else "user"
        content = message.get("content")
        if role == "assistant" and isinstance(content, list):
            tool_calls = anthropic_content_to_chat_tool_calls(content)
            text = anthropic_content_to_text(content)
            if tool_calls:
                messages.append({"role": "assistant", "content": text or "", "tool_calls": tool_calls})
                continue
        text = anthropic_content_to_text(content)
        if text:
            messages.append({"role": role, "content": text})
        messages.extend(anthropic_content_to_tool_messages(content))
    return messages or [{"role": "user", "content": ""}]

def anthropic_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return coerce_text(content)
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            parts.append(coerce_text(part.get("text")))
    return "\n".join(part for part in parts if part)

def anthropic_content_to_chat_tool_calls(content: list[Any]) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": str(part.get("id") or "call_unknown"),
                    "type": "function",
                    "function": {
                        "name": str(part.get("name") or "tool"),
                        "arguments": coerce_json_arguments(part.get("input") or {}),
                    },
                }
            )
    return tool_calls

def anthropic_content_to_tool_messages(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    messages: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(part.get("tool_use_id") or "call_unknown"),
                    "content": response_content_to_text(part.get("content")),
                }
            )
    return messages

def anthropic_tools_to_chat_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    chat_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        chat_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "parameters": tool.get("input_schema") or {},
                },
            }
        )
    return chat_tools

def chat_completion_to_anthropic(chat_response: dict[str, Any], *, model: str, request_id: str) -> dict[str, Any]:
    choice = first_choice(chat_response)
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content_blocks: list[dict[str, Any]] = []
    content = coerce_text(message.get("content"))
    if content:
        content_blocks.append({"type": "text", "text": content})
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": str(tool_call.get("id") or f"toolu_{request_id[-12:]}_{index}"),
                    "name": str(function.get("name") or tool_call.get("name") or "tool"),
                    "input": parse_json_object(function.get("arguments") or tool_call.get("arguments")),
                }
            )
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})
    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": "tool_use" if any(block.get("type") == "tool_use" for block in content_blocks) else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }

def first_choice(chat_response: dict[str, Any]) -> dict[str, Any]:
    choices = chat_response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}

def coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)

def coerce_json_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "{}"
    return json.dumps(value, ensure_ascii=False)

def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {}

def chat_usage_to_response_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
    }

