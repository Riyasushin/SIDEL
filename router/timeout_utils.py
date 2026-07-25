from __future__ import annotations

from typing import Any

MAX_TOOL_TIMEOUT_SECONDS = 300


def clamp_timeout_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return min(value, MAX_TOOL_TIMEOUT_SECONDS)
    return value


def clamp_tool_timeout_fields(payload: Any) -> Any:
    if isinstance(payload, dict):
        clamped: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "timeout":
                clamped[key] = clamp_timeout_value(value)
            else:
                clamped[key] = clamp_tool_timeout_fields(value)
        return clamped
    if isinstance(payload, list):
        return [clamp_tool_timeout_fields(item) for item in payload]
    return payload
