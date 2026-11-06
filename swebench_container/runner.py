from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import time
from pathlib import Path
from code_agents import require_agent_name, resolve_agent_mode

from swebench_container.agents import adapter_for
from swebench_container.models import ContainerRunResult, ContainerRunSpec

PROMPT_ARGUMENT = "__PROMPT_FROM_ARTIFACT__"
POLL_INTERVAL_SECONDS = 0.5


# 容器模块只负责运行任务容器，不理解数据集样本、注入 turn 或 Kimi 配置。
class DockerTaskRunner:
    def build_docker_command(self, spec: ContainerRunSpec) -> list[str]:
        adapter = adapter_for(spec.agent)
        env = adapter.build_env(spec)
        container_name = container_name_for(spec)
        command = [
            "docker",
            "run",
            "--name",
            container_name,
            "--platform",
            spec.platform,
            "--network",
            spec.network,
            "--workdir",
            "/testbed",
            "--label",
            f"code-agent-attack.run_id={spec.run_id}",
            "--label",
            f"code-agent-attack.context_id={spec.context_id}",
            "--volume",
            f"{spec.artifact_dir.resolve()}:/artifacts",
        ]
        for key, value in sorted(env.items()):
            command.extend(["--env", f"{key}={value}"])
        command.append(spec.wrapper_image or spec.swebench_image)
        non_root = require_agent_name(spec.agent) == "claude" and resolve_agent_mode(spec.agent, spec.agent_mode) == "bypassPermissions"
        command.extend(["sh", "-lc", wrapped_agent_script(adapter.build_command(spec), non_root=non_root)])
        return command

    def run(self, spec: ContainerRunSpec) -> ContainerRunResult:
        spec.artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = spec.artifact_dir / "stdout.log"
        stderr_path = spec.artifact_dir / "stderr.log"
        metadata_path = spec.artifact_dir / "metadata.json"
        prompt_path = spec.artifact_dir / "prompt.md"
        exit_path = spec.artifact_dir / "exit.json"
        command = self.build_docker_command(spec)
        container_name = container_name_for(spec)
        metadata_path.write_text(json.dumps({"command": redact_command(command), "spec": serializable_spec(spec)}, indent=2) + "\n", encoding="utf-8")
        prompt_path.write_text(spec.issue_prompt, encoding="utf-8")
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + spec.timeout_seconds
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    inspect_container(container_name, spec.docker_inspect_path or spec.artifact_dir / "docker.inspect.json")
                    remove_container(container_name, force=False)
                    status = "completed" if exit_code == 0 else "agent_crashed"
                    write_exit(exit_path, status=status, exit_code=exit_code, timed_out=False)
                    return ContainerRunResult(spec.context_id, status, exit_code, False, stdout_path, stderr_path, spec.artifact_dir)

                if terminal_artifact_exists(spec):
                    # 中文注释：router 已经对本 context 写入 terminal.json，说明主 agent 收到 409；这里只精确结束当前容器。
                    inspect_container(container_name, spec.docker_inspect_path or spec.artifact_dir / "docker.inspect.json")
                    remove_container(container_name, force=True)
                    exit_code = wait_after_container_removal(process)
                    write_exit(exit_path, status="payload_blocked", exit_code=exit_code, timed_out=False)
                    return ContainerRunResult(spec.context_id, "payload_blocked", exit_code, False, stdout_path, stderr_path, spec.artifact_dir)

                if time.monotonic() >= deadline:
                    inspect_container(container_name, spec.docker_inspect_path or spec.artifact_dir / "docker.inspect.json")
                    remove_container(container_name, force=True)
                    wait_after_container_removal(process)
                    write_exit(exit_path, status="agent_timeout", exit_code=None, timed_out=True)
                    return ContainerRunResult(spec.context_id, "agent_timeout", None, True, stdout_path, stderr_path, spec.artifact_dir)

                time.sleep(POLL_INTERVAL_SECONDS)


def wrapped_agent_script(agent_command: list[str], *, non_root: bool = False) -> str:
    quoted_command = quote_agent_command(agent_command)
    script = "\n".join(
        [
            "set +e",
            "mkdir -p ${CODEX_HOME:-/tmp/codex-home} ${CLAUDE_CONFIG_DIR:-/tmp/claude-home}",
            'if [ -n "${SIDEL_CURSOR_CONFIG:-}" ]; then mkdir -p "$CURSOR_CONFIG_DIR"; printf "%s" "$SIDEL_CURSOR_CONFIG" > "$CURSOR_CONFIG_DIR/cli-config.json"; fi',
            quoted_command,
            "agent_exit=$?",
            "git diff --binary > /artifacts/final.patch 2>/artifacts/git.diff.stderr || true",
            "git status --short > /artifacts/git.status.txt 2>/artifacts/git.status.stderr || true",
            "exit ${agent_exit}",
        ]
    )
    if non_root:
        # Claude's native bypassPermissions mode refuses to run as root.
        return "\n".join([
            "set -e",
            "chown -R sidel /testbed /artifacts",
            f"runuser -u sidel -- sh -c {shlex.quote(script)}",
        ])
    return script


def quote_agent_command(agent_command: list[str]) -> str:
    parts = []
    for argument in agent_command:
        if argument == PROMPT_ARGUMENT:
            parts.append('"$(cat /artifacts/prompt.md)"')
        else:
            parts.append(shlex.quote(argument))
    return " ".join(parts)


def inspect_container(container_name: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(["docker", "inspect", container_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    payload = {
        "container_name": container_name,
        "exit_code": completed.returncode,
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
        "inspect": redact_value(json.loads(completed.stdout.decode("utf-8"))) if completed.returncode == 0 and completed.stdout else None,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def remove_container(container_name: str, *, force: bool) -> None:
    command = ["docker", "rm"]
    if force:
        command.append("--force")
    command.append(container_name)
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def terminal_artifact_exists(spec: ContainerRunSpec) -> bool:
    trace_root = spec.artifact_dir.parent / "router" / "traces"
    return any(trace_root.glob("*/terminal.json"))


def wait_after_container_removal(process: subprocess.Popen) -> int | None:
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    previous = ""
    for item in command:
        if previous == "--env" and is_secret_env(item):
            key = item.split("=", 1)[0]
            redacted.append(f"{key}=<redacted>")
        else:
            redacted.append(item)
        previous = item
    return redacted


def is_secret_env(value: str) -> bool:
    key = value.split("=", 1)[0].upper()
    return key in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ROUTER_API_KEY", "KIMI_API_KEY", "DEEPSEEK_API_KEY", "CURSOR_API_KEY"}


def redact_value(value):
    if isinstance(value, dict):
        return {key: ("<redacted>" if is_secret_env(key) else redact_value(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_env_string(item) if isinstance(item, str) else redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_env_string(value)
    return value


def redact_env_string(value: str) -> str:
    if is_secret_env(value):
        return f"{value.split('=', 1)[0]}=<redacted>"
    return value


def write_exit(path: Path, *, status: str, exit_code: int | None, timed_out: bool) -> None:
    path.write_text(json.dumps({"status": status, "exit_code": exit_code, "timed_out": timed_out}, indent=2) + "\n", encoding="utf-8")


def serializable_spec(spec: ContainerRunSpec) -> dict[str, object]:
    data = dict(spec.__dict__)
    data["artifact_dir"] = str(spec.artifact_dir)
    if spec.docker_inspect_path is not None:
        data["docker_inspect_path"] = str(spec.docker_inspect_path)
    data["router_api_key"] = "<redacted>"
    data["environment"] = redact_value(spec.environment)
    return data


def container_name_for(spec: ContainerRunSpec) -> str:
    context_token = safe_token(spec.context_id)
    suffix = hashlib.sha256(spec.context_id.encode("utf-8")).hexdigest()[:12]
    return f"swebench-{safe_token(spec.run_id)}-{context_token[:67]}-{suffix}"


def safe_token(value: str) -> str:
    chars = [char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value]
    return "".join(chars).strip("-._") or "unknown"
