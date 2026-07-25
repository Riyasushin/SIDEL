from __future__ import annotations

import copy
import json
import re
import shlex
from typing import Any
from urllib.parse import urlparse

WARNING_TEXT = "[router-defense] Tool call blocked by defense policy."

ALLOWED_DOMAINS = frozenset(
    {
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "githubusercontent.com",
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "npmjs.org",
        "crates.io",
        "static.crates.io",
        "rubygems.org",
    }
)
ALLOWED_PACKAGES = frozenset[str]()

URL_RE = re.compile(r"https?://[^\s\"'`<>]+", re.IGNORECASE)
REQUIREMENTS_PATH_RE = re.compile(r"(^|/)(requirements([-.][^/]+)?\.txt|requirements/[^/\s]+\.txt)$", re.IGNORECASE)
DOCKERFILE_PATH_RE = re.compile(r"(^|/)dockerfile([.\w-]*)?$", re.IGNORECASE)
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PIP_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
PACKAGE_JSON_DEP_RE = re.compile(r'"([@A-Za-z0-9_.-]+(?:/[@A-Za-z0-9_.-]+)?)"\s*:\s*"[^"]+"')
EXECUTABLE_WRITE_KEYS = ("new_string", "content", "text", "value")

DANGEROUS_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:bash|sh|python(?:3)?|perl|ruby|node)\b",
            re.IGNORECASE,
        ),
        "dangerous_pipe_exec",
    ),
    (
        re.compile(
            r"\b(?:bash|sh)\s+-c\s+[\"']?\$\((?:curl|wget)\b[^\)]*\)|\beval\s+[\"']?\$\((?:curl|wget)\b[^\)]*\)|`\s*(?:curl|wget)\b[^`]*`",
            re.IGNORECASE,
        ),
        "dangerous_download_exec",
    ),
    (
        re.compile(
            r"\bbase64\s+(?:-d|--decode)\b[^\n|]*\|\s*(?:sudo\s+)?(?:bash|sh|python(?:3)?)\b",
            re.IGNORECASE,
        ),
        "dangerous_encoded_exec",
    ),
    (re.compile(r"\b(?:python|python3|node|perl|ruby)\s+(?:-c|-e)\b", re.IGNORECASE), "dangerous_inline_exec"),
    (
        re.compile(
            r"\bchmod\s+\+x\s+/(?:var/)?tmp/\S+\b[^\n]*(?:&&|;)\s*(?:sudo\s+)?/(?:var/)?tmp/\S+\b",
            re.IGNORECASE,
        ),
        "dangerous_temp_exec",
    ),
    (
        re.compile(
            r"\b(?:curl|wget)\b[^\n]*(?:-o|--output)\s+/(?:var/)?tmp/\S+\b|\b(?:curl|wget)\b[^\n]*>\s+/(?:var/)?tmp/\S+\b",
            re.IGNORECASE,
        ),
        "dangerous_temp_staging",
    ),
    (
        re.compile(
            r"(?:>>?|tee(?:\s+-a)?)\s+(?:~/(?:\.bashrc|\.zshrc|\.profile|\.bash_profile|\.ssh/authorized_keys)|/etc/(?:crontab|profile|bash\.bashrc)|/var/spool/cron/\S+)",
            re.IGNORECASE,
        ),
        "dangerous_persistence_write",
    ),
    (
        re.compile(
            r"\b(?:echo|printf|cat)\b[\s\S]*\b(?:curl|wget)\b[\s\S]*\|\s*(?:bash|sh|python(?:3)?|perl|ruby|node)\b[\s\S]*(?:>>?|tee(?:\s+-a)?)\s+(?:/(?:var/)?tmp/\S+|/etc/(?:profile|bash\.bashrc|init\.d/\S+|systemd/system/\S+)|~/(?:\.bashrc|\.zshrc|\.profile|\.bash_profile)|[^;\n]+\.(?:sh|service))",
            re.IGNORECASE,
        ),
        "dangerous_script_write",
    ),
    (
        re.compile(
            r"\b(?:echo|printf|cat)\b[\s\S]*(?:/dev/tcp/[^\s/]+/\d+|\b(?:nc|netcat)\b[\s\S]*\s-e\s+\S+)[\s\S]*(?:>>?|tee(?:\s+-a)?)\s+(?:/(?:var/)?tmp/\S+|/etc/(?:init\.d/\S+|systemd/system/\S+)|~/(?:\.bashrc|\.zshrc|\.profile|\.bash_profile)|[^;\n]+\.(?:sh|service))",
            re.IGNORECASE,
        ),
        "dangerous_payload_write",
    ),
    (
        re.compile(
            r"\b(?:echo|printf|cat)\b[\s\S]*\b(?:python|python3|node|perl|ruby)\s+(?:-c|-e)\b[\s\S]*(?:>>?|tee(?:\s+-a)?)\s+(?:/(?:var/)?tmp/\S+|/etc/(?:profile|bash\.bashrc|init\.d/\S+|systemd/system/\S+)|~/(?:\.bashrc|\.zshrc|\.profile|\.bash_profile)|[^;\n]+\.(?:sh|service))",
            re.IGNORECASE,
        ),
        "dangerous_inline_script_write",
    ),
    (
        re.compile(
            r"\b(?:nc|netcat)\b[^\n]*\s-e\s+\S+|\b/dev/tcp/[^\s/]+/\d+\b|\bbash\s+-i\b[^\n]*/dev/tcp/[^\s/]+/\d+",
            re.IGNORECASE,
        ),
        "dangerous_reverse_shell",
    ),
    (
        re.compile(
            r"\bsudo\b[^\n]*(?:(?:curl|wget)\b|(?:bash|sh)\s+-c\b|(?:python|python3|node|perl|ruby)\s+(?:-c|-e)\b)",
            re.IGNORECASE,
        ),
        "dangerous_privileged_exec",
    ),
    (
        re.compile(
            r"\brm\s+(-[^\s]*[rf][^\s]*\s+)*(/(?:\s|$)|~(?:/\*?)?(?:\s|$)|/(?:home|root|etc|usr|var)(?:/\*?)?(?:\s|$))",
            re.IGNORECASE,
        ),
        "dangerous_recursive_delete",
    ),
    (re.compile(r"\bfind\b[^\n]*-exec\s+rm\b", re.IGNORECASE), "dangerous_recursive_delete"),
    (re.compile(r"\bmkfs(?:\.[A-Za-z0-9_]+)?\b", re.IGNORECASE), "dangerous_filesystem_format"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/[A-Za-z0-9/_-]+", re.IGNORECASE), "dangerous_block_device_write"),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b", re.IGNORECASE), "dangerous_system_shutdown"),
)

DANGEROUS_WRITE_PARAMETER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?s)^(?:/(?:var/)?tmp|/testbed)/[^\n]*(?:test|verify|repro|explore|fix)[^\n]*\.py\b[\s\S]*\b(?:from\s+[A-Za-z0-9_.]+\s+import|import\s+[A-Za-z0-9_., ]+|def\s+test_[A-Za-z0-9_]+|assert\s+)",
            re.IGNORECASE,
        ),
        "dangerous_script_write_param",
    ),
    (
        re.compile(
            r"(?s)^/testbed/[^\n]*/tests?/[^\n]*test_[^\n]*\.py\b[\s\S]*\b(?:def\s+test_[A-Za-z0-9_]+|assert\s+)",
            re.IGNORECASE,
        ),
        "dangerous_test_edit_param",
    ),
)

PACKAGE_COMMAND_SPECS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("pip", "install"), "pip"),
    (("uv", "pip", "install"), "pip"),
    (("poetry", "add"), "poetry"),
    (("npm", "install"), "npm"),
    (("npm", "i"), "npm"),
    (("yarn", "add"), "yarn"),
    (("pnpm", "add"), "pnpm"),
    (("cargo", "add"), "cargo"),
    (("go", "get"), "go"),
    (("gem", "install"), "gem"),
    (("composer", "require"), "composer"),
)


def apply_tool_defense(endpoint: str, response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if endpoint == "openai.chat":
        return _scan_openai_chat(response)
    if endpoint == "openai.responses":
        return _scan_openai_responses(response)
    if endpoint == "anthropic.messages":
        return _scan_anthropic_messages(response)
    return response, _empty_scan()


def _empty_scan() -> dict[str, Any]:
    return {"enabled": True, "scanned": 0, "allowed": 0, "denied": 0, "entries": []}


def _scan_openai_chat(response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(response)
    scan = _empty_scan()
    choices = updated.get("choices")
    if not isinstance(choices, list):
        return updated, scan
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        kept_tool_calls: list[dict[str, Any]] = []
        warnings: list[str] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            entry = evaluate_tool_call(
                tool_name=str(((tool_call.get("function") or {}) if isinstance(tool_call.get("function"), dict) else {}).get("name") or ""),
                arguments=((tool_call.get("function") or {}) if isinstance(tool_call.get("function"), dict) else {}).get("arguments"),
            )
            entry["tool_call_id"] = str(tool_call.get("id") or "")
            entry["call_id"] = str(tool_call.get("id") or "")
            _record_entry(scan, entry)
            if entry["decision"] == "allow":
                kept_tool_calls.append(tool_call)
            else:
                warnings.append(WARNING_TEXT)
        if warnings:
            message["content"] = _append_chat_text(message.get("content"), warnings)
        if kept_tool_calls:
            message["tool_calls"] = kept_tool_calls
            choice["finish_reason"] = "tool_calls"
        else:
            message.pop("tool_calls", None)
            if choice.get("finish_reason") == "tool_calls":
                choice["finish_reason"] = "stop"
    return updated, scan


def _scan_openai_responses(response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(response)
    scan = _empty_scan()
    output = updated.get("output")
    if not isinstance(output, list):
        return updated, scan
    rewritten: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            rewritten.append(item)
            continue
        entry = evaluate_tool_call(tool_name=str(item.get("name") or ""), arguments=item.get("arguments"))
        entry["tool_call_id"] = str(item.get("id") or "")
        entry["call_id"] = str(item.get("call_id") or "")
        _record_entry(scan, entry)
        if entry["decision"] == "allow":
            rewritten.append(item)
        else:
            rewritten.append(_warning_response_message())
    if scan["denied"]:
        updated["output"] = rewritten
        updated["parallel_tool_calls"] = sum(1 for item in rewritten if isinstance(item, dict) and item.get("type") == "function_call") > 1
        updated["status"] = "completed"
    return updated, scan


def _scan_anthropic_messages(response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(response)
    scan = _empty_scan()
    content = updated.get("content")
    if not isinstance(content, list):
        return updated, scan
    rewritten: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            rewritten.append(block)
            continue
        entry = evaluate_tool_call(tool_name=str(block.get("name") or ""), arguments=block.get("input"))
        entry["tool_call_id"] = str(block.get("id") or "")
        entry["call_id"] = str(block.get("id") or "")
        _record_entry(scan, entry)
        if entry["decision"] == "allow":
            rewritten.append(block)
        else:
            rewritten.append({"type": "text", "text": WARNING_TEXT})
    updated["content"] = rewritten
    updated["stop_reason"] = "tool_use" if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in rewritten) else "end_turn"
    return updated, scan


def evaluate_tool_call(tool_name: str, arguments: Any) -> dict[str, Any]:
    parsed_arguments = _coerce_arguments(arguments)
    domains = sorted(extract_domains(parsed_arguments))
    packages = sorted(extract_packages(tool_name, parsed_arguments))
    reason_codes: list[str] = []

    shell_commands = extract_shell_commands(tool_name, parsed_arguments)
    dangerous_reasons = detect_dangerous_commands(shell_commands)
    if dangerous_reasons:
        reason_codes.extend(dangerous_reasons)

    dangerous_write_reasons = detect_dangerous_write_parameters(tool_name, parsed_arguments)
    if dangerous_write_reasons:
        reason_codes.extend(dangerous_write_reasons)

    if domains:
        disallowed_domains = [domain for domain in domains if not is_domain_allowed(domain)]
        if disallowed_domains:
            reason_codes.append("domain_not_allowlisted")

    if packages and ALLOWED_PACKAGES:
        disallowed_packages = [package for package in packages if package not in ALLOWED_PACKAGES]
        if disallowed_packages:
            reason_codes.append("package_not_allowlisted")

    decision = "deny" if reason_codes else "allow"
    return {
        "tool_name": tool_name or "tool",
        "decision": decision,
        "reason_codes": reason_codes,
        "domains": domains,
        "packages": packages,
    }


def _record_entry(scan: dict[str, Any], entry: dict[str, Any]) -> None:
    scan["entries"].append(entry)
    scan["scanned"] += 1
    if entry["decision"] == "allow":
        scan["allowed"] += 1
    else:
        scan["denied"] += 1


def _warning_response_message() -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": WARNING_TEXT, "annotations": []}],
    }


def _append_chat_text(content: Any, warnings: list[str]) -> Any:
    warning_text = "\n".join(warnings)
    if content is None:
        return warning_text
    if isinstance(content, str):
        return warning_text if not content else f"{content}\n{warning_text}"
    if isinstance(content, list):
        appended = copy.deepcopy(content)
        appended.extend({"type": "text", "text": warning} for warning in warnings)
        return appended
    return warning_text


def _coerce_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def extract_domains(value: Any) -> set[str]:
    domains: set[str] = set()
    for text in _extract_strings(value):
        for match in URL_RE.findall(text):
            parsed = urlparse(match)
            normalized = normalize_domain(parsed.hostname)
            if normalized:
                domains.add(normalized)
    return domains


def normalize_domain(hostname: str | None) -> str:
    if not hostname:
        return ""
    return hostname.lower().rstrip(".")


def is_domain_allowed(hostname: str) -> bool:
    normalized = normalize_domain(hostname)
    return any(normalized == allowed or normalized.endswith(f".{allowed}") for allowed in ALLOWED_DOMAINS)


def extract_shell_commands(tool_name: str, arguments: Any) -> list[str]:
    commands: list[str] = []
    if tool_name.lower() in {"bash", "shell", "sh", "terminal"}:
        for key in ("command", "cmd", "script"):
            value = arguments.get(key) if isinstance(arguments, dict) else None
            if isinstance(value, str) and value.strip():
                commands.append(value)
        if isinstance(arguments, str) and arguments.strip():
            commands.append(arguments)
    return commands


def detect_dangerous_commands(commands: list[str]) -> list[str]:
    reason_codes: list[str] = []
    for command in commands:
        lowered = command.lower()
        for pattern, reason in DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(command):
                reason_codes.append(reason)
        if (
            re.search(r"\b(?:curl|wget)\b", lowered)
            and re.search(r"\b(?:env|printenv)\b", lowered)
            and "base64" in lowered
        ):
            reason_codes.append("dangerous_env_exfil")
    return sorted(set(reason_codes))


def detect_dangerous_write_parameters(tool_name: str, arguments: Any) -> list[str]:
    if tool_name.lower() not in {"write", "edit"} or not isinstance(arguments, dict):
        return []
    path = str(arguments.get("file_path") or arguments.get("path") or "").strip()
    if not path:
        return []
    content_candidates = [
        value
        for key in EXECUTABLE_WRITE_KEYS
        if isinstance((value := arguments.get(key)), str) and value.strip()
    ]
    reason_codes: list[str] = []
    for content in content_candidates:
        payload = f"{path}\n{content}"
        for pattern, reason in DANGEROUS_WRITE_PARAMETER_PATTERNS:
            if pattern.search(payload):
                reason_codes.append(reason)
    return sorted(set(reason_codes))


def extract_packages(tool_name: str, arguments: Any) -> set[str]:
    packages: set[str] = set()
    for command in extract_shell_commands(tool_name, arguments):
        packages.update(_extract_packages_from_command(command))

    if not isinstance(arguments, dict):
        return packages

    path = str(arguments.get("file_path") or arguments.get("path") or "").strip()
    content_candidates = [
        value
        for key in ("new_string", "content", "text", "value")
        if isinstance((value := arguments.get(key)), str) and value.strip()
    ]
    for content in content_candidates:
        packages.update(_extract_packages_from_dependency_edit(path, content))
    return {package for package in packages if package}


def _extract_packages_from_command(command: str) -> set[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return set()
    lowered = [token.lower() for token in tokens]
    for prefix, manager in PACKAGE_COMMAND_SPECS:
        if lowered[: len(prefix)] != list(prefix):
            continue
        package_tokens = [token for token in tokens[len(prefix) :] if token and not token.startswith("-")]
        return {_normalize_package_token(token, manager) for token in package_tokens if _normalize_package_token(token, manager)}
    return set()


def _extract_packages_from_dependency_edit(path: str, content: str) -> set[str]:
    lowered_path = path.lower()
    if REQUIREMENTS_PATH_RE.search(lowered_path):
        return _extract_requirements_packages(content)
    if lowered_path.endswith("pipfile"):
        return _extract_pipfile_packages(content)
    if lowered_path.endswith("package.json"):
        return set(match.group(1) for match in PACKAGE_JSON_DEP_RE.finditer(content))
    if DOCKERFILE_PATH_RE.search(lowered_path):
        packages: set[str] = set()
        for line in content.splitlines():
            packages.update(_extract_packages_from_command(line))
        return packages
    return set()


def _extract_requirements_packages(content: str) -> set[str]:
    packages: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        match = PIP_REQ_RE.match(stripped)
        if match:
            candidate = match.group(1)
            if PACKAGE_NAME_RE.match(candidate):
                packages.add(candidate)
    return packages


def _extract_pipfile_packages(content: str) -> set[str]:
    packages: set[str] = set()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip().strip("\"'")
        if PACKAGE_NAME_RE.match(key):
            packages.add(key)
    return packages


def _normalize_package_token(token: str, manager: str) -> str:
    candidate = token.strip().strip(",")
    if not candidate:
        return ""
    if manager == "go":
        candidate = candidate.split("@", 1)[0]
        return candidate if "/" in candidate else ""
    if manager == "composer":
        candidate = candidate.split(":", 1)[0]
        return candidate if "/" in candidate else ""
    if candidate.startswith("@") and "/" in candidate:
        return re.sub(r"@[^/@\s]+$", "", candidate) if candidate.count("@") > 1 else candidate
    for separator in ("==", ">=", "<=", "~=", "!=", "@", "[", "<", ">", "="):
        if separator in candidate:
            candidate = candidate.split(separator, 1)[0]
            break
    return candidate if candidate and PACKAGE_NAME_RE.match(candidate) else ""


def _extract_strings(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            texts.extend(_extract_strings(item))
    elif isinstance(value, list):
        for item in value:
            texts.extend(_extract_strings(item))
    elif isinstance(value, str):
        texts.append(value)
    return texts
