import hashlib
import json
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from router.context import ExperimentContext

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
_TRACE_TAG_RE = re.compile(r"\[router-trace\]\n.*?\[/router-trace\]", re.DOTALL)
_CLAUDE_BILLING_HEADER_RE = re.compile(r"x-anthropic-billing-header:.*?(?=\n|$)")
_DYNAMIC_USER_ID_KEYS = {"device_id", "session_id"}
_DYNAMIC_KEYS = {"id", "trace_id", "request_id", "created", "created_at", "ts"}
_TURN_COUNTERS: dict[str, int] = defaultdict(int)
_INJECTABLE_TURN_COUNTERS: dict[str, int] = defaultdict(int)
_TASK_TURN_COUNTERS: dict[str, int] = {}
_TURN_LOCK = threading.Lock()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _SECRET_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_PATTERN.sub("sk-<redacted>", value)
    return value


def stable_hash(value: Any) -> str:
    normalized = normalize_for_hash(redact(value))
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_for_hash(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize_for_hash(item, (*path, key))
            for key, item in value.items()
            if key not in _DYNAMIC_KEYS
        }
    if isinstance(value, list):
        return [normalize_for_hash(item, (*path, "[]")) for item in value]
    if isinstance(value, str):
        if path == ("metadata", "user_id"):
            return normalize_claude_user_id(value)
        normalized = _TRACE_TAG_RE.sub("[router-trace]\nRUN_ID=<run>\nAGENT=<agent>\nINSTANCE_ID=<instance>\n[/router-trace]", value)
        return _CLAUDE_BILLING_HEADER_RE.sub("x-anthropic-billing-header: <normalized>", normalized)
    return value


def normalize_claude_user_id(value: str) -> str:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(payload, dict):
        return value
    normalized = {
        key: ("<normalized>" if key in _DYNAMIC_USER_ID_KEYS else item)
        for key, item in payload.items()
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# turn 计数必须按 context 和请求角色隔离；reviewer 请求不能顶乱主 agent 的 replay turn。
def next_turn_index(context: ExperimentContext, request_role: str) -> int:
    key = f"{context.run_id}:{context.context_id}:{request_role}"
    with _TURN_LOCK:
        turn = _TURN_COUNTERS[key]
        _TURN_COUNTERS[key] += 1
    return turn


def next_injectable_turn_index(context: ExperimentContext) -> int:
    key = f"{context.run_id}:{context.context_id}:agent_main"
    with _TURN_LOCK:
        turn = _INJECTABLE_TURN_COUNTERS[key]
        _INJECTABLE_TURN_COUNTERS[key] += 1
    return turn


def next_task_turn_index(context: ExperimentContext, endpoint: str, body: dict[str, Any], request_role: str) -> int | None:
    if request_role != "agent_main" or endpoint not in {"openai.chat", "openai.responses", "anthropic.messages"}:
        return None
    key = f"{context.run_id}:{context.context_id}:agent_main"
    with _TURN_LOCK:
        if key not in _TASK_TURN_COUNTERS:
            if not is_task_start_request(endpoint, body, context):
                return None
            _TASK_TURN_COUNTERS[key] = 0
        turn = _TASK_TURN_COUNTERS[key]
        _TASK_TURN_COUNTERS[key] += 1
        return turn


def rollback_task_turn_index(context: ExperimentContext, metadata: dict[str, Any]) -> None:
    """Keep a failed upstream attempt from consuming the injection turn.

    Context requests are serialized by ``request_gate``, so a failed task turn
    is always the most recently allocated one and can safely be retried with
    the same task-turn index.
    """

    if metadata.get("request_role") != "agent_main":
        return
    task_turn = metadata.get("task_turn_index")
    if task_turn is None:
        return
    key = f"{context.run_id}:{context.context_id}:agent_main"
    expected_next = int(task_turn) + 1
    with _TURN_LOCK:
        current_next = _TASK_TURN_COUNTERS.get(key)
        if current_next != expected_next:
            raise RuntimeError(
                f"cannot roll back task turn for {context.context_id}: expected next={expected_next}, got {current_next}"
            )
        _TASK_TURN_COUNTERS[key] = int(task_turn)


def is_injectable_request(endpoint: str, body: dict[str, Any], request_role: str) -> bool:
    if request_role != "agent_main":
        return False
    tools = body.get("tools")
    return endpoint in {"openai.chat", "openai.responses", "anthropic.messages"} and isinstance(tools, list) and bool(tools)


def is_task_start_request(endpoint: str, body: dict[str, Any], context: ExperimentContext) -> bool:
    if context.agent == "claude" and endpoint == "anthropic.messages" and looks_like_claude_title_request(body):
        return False
    return has_user_prompt_message(body)


def looks_like_claude_title_request(body: dict[str, Any]) -> bool:
    text = json.dumps(body, ensure_ascii=False)
    if "Generate a concise, sentence-case title" in text:
        return True
    output_config = body.get("output_config")
    if not isinstance(output_config, dict):
        return False
    output_format = output_config.get("format")
    if not isinstance(output_format, dict):
        return False
    schema = output_format.get("schema")
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    return isinstance(properties, dict) and "title" in properties


def has_user_prompt_message(body: dict[str, Any]) -> bool:
    messages = body.get("messages")
    if isinstance(messages, list):
        return any(message_has_user_text(message) for message in messages)
    prompt_input = body.get("input")
    if isinstance(prompt_input, list):
        return any(message_has_user_text(message) for message in prompt_input)
    return isinstance(prompt_input, str) and bool(prompt_input.strip())


def message_has_user_text(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    return bool(extract_message_text(message.get("content")).strip())


def extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                chunks.append(str(item.get("text") or ""))
        return "\n".join(chunks)
    return ""


def reset_turn_counters() -> None:
    with _TURN_LOCK:
        _TURN_COUNTERS.clear()
        _INJECTABLE_TURN_COUNTERS.clear()
        _TASK_TURN_COUNTERS.clear()


def extract_trace_metadata(body: dict[str, Any], context: ExperimentContext, endpoint: str) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    request_role = infer_request_role(endpoint, body, context)
    injectable_turn_index = next_injectable_turn_index(context) if is_injectable_request(endpoint, body, request_role) else None
    task_turn_index = next_task_turn_index(context, endpoint, body, request_role)
    return {
        "trace_id": f"trace_{uuid.uuid4().hex}",
        "run_id": context.run_id,
        "context_id": context.context_id,
        "turn_index": next_turn_index(context, request_role),
        "injectable_turn_index": injectable_turn_index,
        "task_turn_index": task_turn_index,
        "endpoint": endpoint,
        "agent": context.agent,
        "agent_mode": context.agent_mode,
        "instance_id": context.instance_id,
        "request_role": request_role,
        "request_hash": stable_hash(body),
        "started_at": started_at,
        "router_mode": context.router_mode,
        "trace_source_dir": context.trace_source_dir,
        "router_api_key_hash": context.api_key_hash,
    }


# reviewer 分类只用于分析归因，不作为是否执行工具调用的判定依据。
def infer_request_role(endpoint: str, body: dict[str, Any], context: ExperimentContext) -> str:
    model = str(body.get("model") or "").lower()
    if context.agent == "codex" and endpoint == "openai.responses" and "review" in model:
        return "reviewer"
    if context.agent == "claude" and endpoint == "anthropic.messages" and looks_like_claude_auto_review(body):
        return "reviewer"
    if "review" in model:
        return "reviewer"
    return "agent_main"


def looks_like_claude_auto_review(body: dict[str, Any]) -> bool:
    text = json.dumps(body, ensure_ascii=False)
    markers = (
        "<transcript>",
        "HARD BLOCK",
        "SOFT BLOCK",
        "Classification Process",
        "Output Format",
        "<block>yes</block>",
        "<block>no</block>",
    )
    return sum(marker in text for marker in markers) >= 2


def write_trace_event(context: ExperimentContext, metadata: dict[str, Any], event: str, payload: dict[str, Any]) -> None:
    if not context.trace_dir:
        return
    path = Path(context.trace_dir)
    path.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **metadata,
        **redact(payload),
    }
    with (path / "router-full.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_turn_artifact(context: ExperimentContext, metadata: dict[str, Any], name: str, payload: Any) -> Path | None:
    if not context.trace_dir:
        return None
    path = turn_artifact_dir(context, metadata)
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    with target.open("w", encoding="utf-8") as fh:
        json.dump(redact(payload), fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return target


def turn_artifact_dir(context: ExperimentContext, metadata: dict[str, Any]) -> Path:
    turn = int(metadata.get("turn_index") or 0)
    endpoint = safe_path_part(str(metadata.get("endpoint") or "unknown"))
    request_role = str(metadata.get("request_role") or "agent_main")
    if request_role == "agent_main":
        return Path(context.trace_dir) / f"{turn:04d}-{endpoint}"
    return Path(context.trace_dir) / f"{turn:04d}-{safe_path_part(request_role)}-{endpoint}"


def safe_path_part(value: str) -> str:
    chars = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            chars.append(char)
        else:
            chars.append("-")
    cleaned = "".join(chars).strip("-._")
    return cleaned or "unknown"
