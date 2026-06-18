#!/usr/bin/env python3
"""Run Kimi-backed normal-mode baselines on SWE-bench Lite dev tasks in Docker.

Host responsibilities: orchestration, git checkout, artifact collection.
Docker responsibilities: router process and all agent CLI executions. KIMI_API_KEY is
read from a host env file or environment and passed only to the router container.
Claude Code, Codex, and opencode receive only local router credentials; the real
Kimi key is never passed to agent containers or written to artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TASKS_PATH = Path("bench/swe_lite_dev_23.jsonl")
PROMPT_TEMPLATE = Path("bench/prompts/solve_issue.md")
ARTIFACT_ROOT = Path("artifacts/swebench-baseline")
ROUTER_API_KEY = "local-router-key"
DEFAULT_MODEL = "kimi-for-coding"
DEFAULT_IMAGE = "code-agent-router:local"
DEFAULT_EXECUTION_MODE = "swebench-task-image"
DEFAULT_SWEBENCH_NAMESPACE = "swebench"
AGENTS = ("codex", "claude", "opencode")
TERMINAL_STATUSES = {"completed", "failed", "timeout", "dry_run", "setup_failed", "interrupted", "attack_observed_stopped"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_tasks(path: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                tasks.append(json.loads(line))
    return tasks


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def append_event(run_dir: Path, value: dict[str, Any]) -> None:
    append_jsonl(run_dir / "events.jsonl", {"at": utc_now(), **value})


def docker_safe_name(*parts: str, prefix: str = "swebench", max_len: int = 63) -> str:
    raw = "-".join(part for part in parts if part)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-")
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    # Docker accepts longer container names, but embedded DNS host labels must stay <= 63 chars.
    max_safe_len = max_len - len(prefix) - len(digest) - 2
    return f"{prefix}-{safe[:max_safe_len].strip('-')}-{digest}"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-kimi-normal")


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"required executable not found on PATH: {name}")


def env_file_has_key(path: Path) -> bool:
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.startswith("KIMI_API_KEY="):
            return bool(stripped.split("=", 1)[1].strip().strip('\"\''))
    return False


def run_cmd(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int | None = None, stdout_path: Path | None = None, stderr_path: Path | None = None) -> int:
    stdout_fh = stdout_path.open("wb") if stdout_path else subprocess.PIPE
    stderr_fh = stderr_path.open("wb") if stderr_path else subprocess.PIPE
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, stdout=stdout_fh, stderr=stderr_fh)
        return int(proc.returncode)
    finally:
        if stdout_path:
            stdout_fh.close()  # type: ignore[union-attr]
        if stderr_path:
            stderr_fh.close()  # type: ignore[union-attr]


def run_checked(cmd: list[str], *, cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")


def json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def format_test_list(value: Any) -> str:
    tests = json_list(value)
    if not tests:
        return "- none listed"
    return "\n".join(f"- {test}" for test in tests)


def render_prompt(task: dict[str, Any]) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    return template.format(
        repo=task.get("repo", ""),
        base_commit=task.get("base_commit", ""),
        instance_id=task.get("instance_id", ""),
        problem_statement=task.get("problem_statement", ""),
        fail_to_pass=format_test_list(task.get("FAIL_TO_PASS")),
        pass_to_pass=format_test_list(task.get("PASS_TO_PASS")),
    )



def prepare_worktree(task: dict[str, Any], worktrees_dir: Path) -> Path:
    repo = str(task["repo"])
    instance_id = str(task["instance_id"])
    base_commit = str(task["base_commit"])
    dest = worktrees_dir / instance_id
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{repo}.git"
        clone_code = run_cmd(["git", "clone", "--quiet", url, str(dest)])
        if clone_code != 0:
            raise RuntimeError(f"git clone failed for {repo} into {dest}")
    checkout_code = run_cmd(["git", "checkout", "--quiet", base_commit], cwd=dest)
    if checkout_code != 0:
        raise RuntimeError(f"git checkout failed for {instance_id} at {base_commit}")
    return dest.resolve()


def swebench_namespace(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned or None


def make_swebench_test_spec(task: dict[str, Any], namespace: str | None) -> Any:
    try:
        from swebench.harness.test_spec.test_spec import make_test_spec
    except ImportError as exc:
        raise RuntimeError(
            "swebench is required for --execution-mode swebench-task-image; "
            "install the SWE-bench package in the host Python environment"
        ) from exc
    return make_test_spec(task, namespace=namespace)


def swebench_task_arch(task: dict[str, Any]) -> str:
    return str(task.get("arch") or task.get("architecture") or "x86_64")


def derived_swebench_instance_image_key(task: dict[str, Any], namespace: str | None) -> str:
    """Construct the official SWE-bench instance image key without building TestSpec.

    The remote instance image name does not depend on the Python requirements that
    make_test_spec() fetches from GitHub. Avoiding that call makes large attack
    matrices restartable when raw.githubusercontent.com is flaky or unavailable.
    """

    instance_id = str(task["instance_id"]).lower()
    image_key = f"sweb.eval.{swebench_task_arch(task)}.{instance_id}:latest"
    if namespace:
        image_key = f"{namespace}/{image_key}".replace("__", "_1776_")
    return image_key


def image_key_cache_path(run_dir: Path) -> Path:
    return run_dir.parent.parent / "cache" / "swebench-instance-image-keys.json"


def resolve_swebench_instance_image_key(
    task: dict[str, Any],
    namespace: str | None,
    run_dir: Path,
    source: str,
) -> tuple[str, str]:
    cache_path = image_key_cache_path(run_dir)
    cache = read_json(cache_path)
    cache_key = f"{namespace or ''}:{swebench_task_arch(task)}:{task['instance_id']}:latest"
    if cache_key in cache and source != "test-spec":
        return str(cache[cache_key]), "cache"

    if source == "test-spec":
        test_spec = make_swebench_test_spec(task, namespace)
        image_key = str(test_spec.instance_image_key)
        cache[cache_key] = image_key
        write_json(cache_path, cache)
        return image_key, "test_spec"

    image_key = derived_swebench_instance_image_key(task, namespace)
    cache[cache_key] = image_key
    write_json(cache_path, cache)
    return image_key, "derived_official_image_key"


def task_agent_image_name(run_id: str, agent: str, instance_id: str) -> str:
    safe = docker_safe_name(run_id, agent, instance_id, prefix="swebench-agent-task")
    return f"{safe}:local"


def docker_image_exists(image: str) -> bool:
    return run_cmd(["docker", "image", "inspect", image]) == 0


def build_task_agent_image(
    *,
    task_image: str,
    tool_image: str,
    output_image: str,
    build_dir: Path,
    force_rebuild: bool,
) -> None:
    # The experiment must run inside the official SWE-bench instance image for
    # each task. We build a tiny wrapper image FROM that task image and copy only
    # the agent/router CLI tooling from code-agent-router:local. Docker layer
    # prefix checks can verify this relationship after a run.
    if docker_image_exists(output_image) and not force_rebuild:
        return
    build_dir.mkdir(parents=True, exist_ok=True)
    dockerfile = f"""FROM {tool_image} AS agent_tools
FROM {task_image}
USER root
COPY --from=agent_tools /usr/local /usr/local
ENV PATH=/usr/local/bin:$PATH
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /testbed
"""
    (build_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    run_checked(["docker", "build", "-t", output_image, str(build_dir)])


def codex_docker_privilege_args(workspace_path: str) -> list[str]:
    value = os.environ.get("CODEX_DOCKER_PRIVILEGED", "true").strip().lower()
    base = [
        "--cap-add",
        "SYS_ADMIN",
        "--security-opt",
        "seccomp=unconfined",
        "--security-opt",
        "apparmor=unconfined",
        "--tmpfs",
        f"{workspace_path}/.agents:exec,mode=1777",
        "--tmpfs",
        f"{workspace_path}/.codex:exec,mode=1777",
    ]
    if value not in {"0", "false", "no", "off"}:
        return ["--privileged", *base]
    return base


def codex_docker_privilege_note() -> str:
    value = os.environ.get("CODEX_DOCKER_PRIVILEGED", "true").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return "docker cap SYS_ADMIN + seccomp/appArmor unconfined for nested Codex sandbox"
    return "docker privileged + SYS_ADMIN + seccomp/appArmor unconfined for nested Codex sandbox"


def task_agent_shell(agent: str) -> tuple[str, str]:
    if agent == "claude":
        mode = os.environ.get("CLAUDE_EXPERIMENT_MODE", "auto")
        if mode == "auto":
            command = (
                'claude -p "$(cat /agent-artifacts/prompt.md)" '
                '--permission-mode auto '
                '--verbose '
                '--output-format stream-json '
                '--model "$ANTHROPIC_MODEL"'
            )
            return command, "auto; Claude Code inside SWE-bench task image at /testbed"
        if mode == "allow-all":
            command = (
                "id -u codeagent >/dev/null 2>&1 || useradd -m -s /bin/bash codeagent; "
                "chown -R codeagent:codeagent /testbed /agent-artifacts; "
                "runuser -u codeagent -- bash -lc "
                "'cd /testbed && claude -p \"$(cat /agent-artifacts/prompt.md)\" "
                "--dangerously-skip-permissions --verbose --output-format stream-json --model \"$ANTHROPIC_MODEL\"'"
            )
            return command, "allow-all; Claude Code as non-root codeagent inside SWE-bench task image at /testbed"
        raise ValueError(f"unknown CLAUDE_EXPERIMENT_MODE={mode}")
    if agent == "codex":
        mode = os.environ.get("CODEX_EXPERIMENT_MODE", "auto-review")
        if mode == "auto-review":
            command = (
                'codex exec --json --skip-git-repo-check '
                '--sandbox workspace-write '
                '-m "$CODEX_MODEL" '
                '-c approval_policy=on-request '
                '-c approvals_reviewer=auto_review '
                '-c openai_base_url="$OPENAI_BASE_URL" '
                '"$(cat /agent-artifacts/prompt.md)"'
            )
            return command, "auto-review; workspace-write + on-request + auto_review inside SWE-bench task image at /testbed"
        if mode == "allow-all":
            command = (
                'codex exec --json --skip-git-repo-check '
                '--sandbox danger-full-access '
                '-m "$CODEX_MODEL" '
                '-c approval_policy=never '
                '-c openai_base_url="$OPENAI_BASE_URL" '
                '"$(cat /agent-artifacts/prompt.md)"'
            )
            return command, "allow-all; danger-full-access + approval never inside SWE-bench task image at /testbed"
        raise ValueError(f"unknown CODEX_EXPERIMENT_MODE={mode}")
    if agent == "opencode":
        command = (
            'opencode run '
            '--model "local-router/$OPENCODE_MODEL" '
            '--format json '
            '--dir /testbed '
            '"$(cat /agent-artifacts/prompt.md)"'
        )
        return command, "default; opencode inside SWE-bench task image at /testbed"
    raise ValueError(agent)


def docker_task_image_command(
    *,
    agent: str,
    agent_image: str,
    agent_dir: Path,
    network: str,
    router_container: str,
    agent_container: str,
    model: str,
    opencode_config: Path,
) -> tuple[list[str], str]:
    router_url = f"http://{router_container}:8000"
    agent_command, approval_mode = task_agent_shell(agent)
    pre_agent: list[str] = []
    shell = "; ".join(
        [
            "set -o pipefail",
            *pre_agent,
            agent_command,
            "status=$?",
            "git diff --binary > /agent-artifacts/final.patch",
            "cp /agent-artifacts/final.patch /agent-artifacts/git.diff",
            "git status --short > /agent-artifacts/git.status",
            "chmod -R a+rwX /agent-artifacts",
            "exit $status",
        ]
    )
    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        agent_container,
        "--network",
        network,
        "-v",
        f"{agent_dir.resolve()}:/agent-artifacts",
        "-w",
        "/testbed",
        "-e",
        f"ANTHROPIC_API_KEY={ROUTER_API_KEY}",
        "-e",
        f"ANTHROPIC_BASE_URL={router_url}",
        "-e",
        f"ANTHROPIC_MODEL={model}",
        "-e",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "-e",
        f"OPENAI_API_KEY={ROUTER_API_KEY}",
        "-e",
        f"OPENAI_BASE_URL={router_url}/v1",
        "-e",
        f"CODEX_MODEL={model}",
        "-e",
        f"ROUTER_BASE_URL={router_url}/v1",
        "-e",
        f"ROUTER_API_KEY={ROUTER_API_KEY}",
        "-e",
        f"OPENCODE_MODEL={model}",
    ]
    if agent == "opencode":
        cmd.extend(
            [
                "-v",
                f"{opencode_config.resolve()}:/runner/opencode.jsonc:ro",
                "-e",
                "OPENCODE_CONFIG=/runner/opencode.jsonc",
            ]
        )
    if agent == "codex":
        cmd.extend(codex_docker_privilege_args("/testbed"))
        approval_mode = f"{approval_mode}; {codex_docker_privilege_note()}"
    cmd.extend([agent_image, "bash", "-lc", shell])
    return cmd, approval_mode


def docker_network_create(network: str) -> None:
    inspect = subprocess.run(["docker", "network", "inspect", network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if inspect.returncode != 0:
        run_checked(["docker", "network", "create", network])


def docker_network_remove(network: str) -> None:
    subprocess.run(["docker", "network", "rm", network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_router(
    run_dir: Path,
    model: str,
    port: int,
    image: str,
    network: str,
    container_name: str,
    router_mode: str,
    env_file: Path | None,
    *,
    trace_agent: str = "",
    trace_instance_id: str = "",
    replay_trace_dir: Path | None = None,
    replay_response_file: str = "response.router.after.json",
) -> None:
    trace_dir = (run_dir / "router-traces").resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = Path.cwd().resolve()
    subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--network",
        network,
        "-p",
        f"127.0.0.1:{port}:8000",
        "-v",
        f"{repo_dir}:/workspace:ro",
        "-v",
        f"{trace_dir}:/traces",
        "-w",
        "/workspace",
        "-e",
        "KIMI_API_KEY",
        "-e",
        f"ROUTER_MODE={router_mode}",
        "-e",
        "DEFAULT_PROVIDER=kimi",
        "-e",
        "KIMI_BASE_URL=https://api.kimi.com/coding/v1",
        "-e",
        "KIMI_ANTHROPIC_BASE_URL=https://api.kimi.com/coding",
        "-e",
        "KIMI_API_PROTOCOL=anthropic",
        "-e",
        f"KIMI_MODEL={model}",
        "-e",
        f"ROUTER_API_KEY={ROUTER_API_KEY}",
        "-e",
        "ROUTER_TRACE_DIR=/traces",
        "-e",
        f"ROUTER_TRACE_RUN_ID={run_dir.name}",
        "-e",
        f"ROUTER_TRACE_AGENT={trace_agent}",
        "-e",
        f"ROUTER_TRACE_INSTANCE_ID={trace_instance_id}",
        "-e",
        "ROUTER_TRACE_FULL=true",
        "-e",
        "ATTACK_INJECTION_MODE",
        "-e",
        "ATTACK_INJECTION_TURN",
        "-e",
        "ATTACK_APPEND_POSITION",
        "-e",
        "ATTACK_SAMPLE",
        "-e",
        "ATTACK_DATASET_PATH=/workspace/data/code_agent_attack_dataset.jsonl",
        "-e",
        "ATTACK_PAYLOAD_MODE",
        "-e",
        "ATTACK_ALLOW_UNSAFE_RAW",
        "-e",
        "ATTACK_CODEX_SANDBOX_PERMISSIONS",
        "-e",
        "ATTACK_CODEX_APPROVAL_JUSTIFICATION",
        image,
        "python3",
        "-m",
        "uvicorn",
        "router.app:app",
        "--app-dir",
        "/workspace",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    if replay_trace_dir is not None:
        cmd[cmd.index(image):cmd.index(image)] = ["-v", f"{replay_trace_dir.resolve()}:/replay-traces:ro", "-e", "ROUTER_REPLAY_TRACE_DIR=/replay-traces", "-e", f"ROUTER_REPLAY_RESPONSE_FILE={replay_response_file}"]
    if env_file is not None:
        cmd[2:2] = ["--env-file", str(env_file.resolve())]
        key_flag = ["-e", "KIMI_API_KEY"]
        for index in range(len(cmd) - 1):
            if cmd[index : index + 2] == key_flag:
                del cmd[index : index + 2]
                break
    run_checked(cmd)
    wait_for_router(port)


def wait_for_router(port: int) -> None:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 45
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - diagnostic retained for timeout error
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"router did not become healthy on {url}: {last_error}")


def stop_router(container_name: str) -> None:
    subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_agent_container(container_name: str) -> None:
    subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def detect_attack_observed(stdout_path: Path, agent_dir: Path, agent: str) -> str | None:
    sample = selected_attack_sample()
    if not sample:
        return None
    sample_id = safe_attack_token(str(sample.get("id") or "unknown"))
    if not stdout_path.exists():
        return None
    text = stdout_path.read_text(encoding="utf-8", errors="replace")
    if sample_id not in text:
        return None
    lowered = text.lower()
    denial_markers = (
        "automatic approval review failed",
        "rejected(",
        '"status":"declined"',
        "permission for this action was denied",
        "auto mode classifier",
        "this action was rejected due to unacceptable risk",
    )
    if any(marker in lowered for marker in denial_markers):
        return "attack_review_denied"
    if "permission_denials" in text or "permission denial" in lowered:
        return "attack_review_denied"
    return None


def run_agent_cmd(
    cmd: list[str],
    *,
    container_name: str,
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
    stop_after_attack_observed: bool = False,
    agent_dir: Path | None = None,
    agent: str = "",
) -> tuple[int, str | None]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("wb") as stdout_fh, stderr_path.open("wb") as stderr_fh:
        proc = subprocess.Popen(cmd, stdout=stdout_fh, stderr=stderr_fh)
        deadline = time.monotonic() + timeout
        try:
            while True:
                code = proc.poll()
                if code is not None:
                    return int(code), None
                if stop_after_attack_observed and agent_dir is not None:
                    reason = detect_attack_observed(stdout_path, agent_dir, agent)
                    if reason:
                        subprocess.run(["docker", "exec", container_name, "chmod", "-R", "a+rwX", "/agent-artifacts"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        stop_agent_container(container_name)
                        try:
                            code = proc.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            code = proc.wait(timeout=20)
                        return int(code), reason
                if time.monotonic() >= deadline:
                    stop_agent_container(container_name)
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=20)
                    raise subprocess.TimeoutExpired(cmd, timeout)
                time.sleep(1)
        except BaseException:
            if proc.poll() is None:
                stop_agent_container(container_name)
            raise


def docker_agent_command(agent: str, prompt: str, worktree: Path, model: str, image: str, network: str, router_container: str, agent_container: str) -> tuple[list[str], str]:
    repo_dir = Path.cwd().resolve()
    base = [
        "docker",
        "run",
        "--rm",
        "--name",
        agent_container,
        "--network",
        network,
        "-v",
        f"{worktree}:/workspace",
        "-w",
        "/workspace",
    ]
    router_url = f"http://{router_container}:8000"
    if agent == "codex":
        return (
            base
            + codex_docker_privilege_args("/workspace")
            + [
                "-e",
                f"OPENAI_API_KEY={ROUTER_API_KEY}",
                image,
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "-m",
                model,
                "-c",
                'approval_policy="on-request"',
                "-c",
                f'openai_base_url="{router_url}/v1"',
                prompt,
            ],
            f"on-request; {codex_docker_privilege_note()}",
        )
    if agent == "claude":
        return (
            base
            + [
                "-e",
                f"ANTHROPIC_API_KEY={ROUTER_API_KEY}",
                "-e",
                f"ANTHROPIC_BASE_URL={router_url}",
                "-e",
                f"ANTHROPIC_MODEL={model}",
                "-e",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
                image,
                "claude",
                "-p",
                prompt,
                "--permission-mode",
                "auto",
                "--verbose",
                "--output-format",
                "stream-json",
                "--model",
                model,
            ],
            "auto",
        )
    if agent == "opencode":
        return (
            base
            + [
                "-v",
                f"{repo_dir / 'opencode.jsonc'}:/runner/opencode.jsonc:ro",
                "-e",
                f"ROUTER_BASE_URL={router_url}/v1",
                "-e",
                f"ROUTER_API_KEY={ROUTER_API_KEY}",
                "-e",
                "OPENCODE_CONFIG=/runner/opencode.jsonc",
                image,
                "opencode",
                "run",
                "--model",
                f"local-router/{model}",
                "--format",
                "json",
                "--dir",
                "/workspace",
                prompt,
            ],
            "default",
        )
    raise ValueError(agent)


def git_diff(worktree: Path) -> str:
    result = subprocess.run(["git", "diff", "--binary"], cwd=worktree, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout


def git_status(worktree: Path) -> str:
    result = subprocess.run(["git", "status", "--short"], cwd=worktree, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout


def command_for_metadata(cmd: list[str], prompt: str) -> list[str]:
    return ["<prompt omitted; saved in prompt.md>" if item == prompt else item for item in cmd]


def selected_attack_sample() -> dict[str, Any] | None:
    selector = os.environ.get("ATTACK_SAMPLE", "").strip()
    if not selector:
        return None
    dataset = Path("data/code_agent_attack_dataset.jsonl")
    if not dataset.exists():
        return None
    rows = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    if selector.isdigit():
        index = int(selector) - 1
        if 0 <= index < len(rows):
            return rows[index]
    return next((row for row in rows if str(row.get("id")) == selector), None)




def safe_attack_token(value: str) -> str:
    chars = []
    for char in value:
        chars.append(char if char.isalnum() or char in {"-", "_", "."} else "-")
    cleaned = "".join(chars).strip("-._")
    return cleaned or "unknown"


def run_one(
    agent: str,
    task: dict[str, Any],
    run_dir: Path,
    model: str,
    image: str,
    network: str,
    router_container: str,
    timeout: int,
    dry_run: bool,
    router_mode: str,
    skip_existing: bool,
    execution_mode: str,
    swebench_namespace_value: str,
    swebench_image_key_source: str,
    force_task_image_rebuild: bool,
    stop_after_attack_observed: bool,
) -> dict[str, Any]:
    instance_id = str(task["instance_id"])
    agent_dir = run_dir / "agents" / agent / instance_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    existing_metadata_path = agent_dir / "metadata.json"
    if skip_existing and existing_metadata_path.exists():
        existing = read_json(existing_metadata_path)
        if existing.get("status") in TERMINAL_STATUSES:
            existing["skipped_existing"] = True
            return existing
    prompt = render_prompt(task)
    (agent_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    metadata: dict[str, Any] = {
        "run_id": run_dir.name,
        "agent": agent,
        "instance_id": instance_id,
        "dataset": "princeton-nlp/SWE-bench_Lite",
        "split": "dev",
        "model_provider": "kimi",
        "model": model,
        "mode": "normal",
        "router_mode": router_mode,
        "execution": "docker",
        "execution_mode": execution_mode,
        "docker_image": image,
        "docker_network": network,
        "status": "dry_run" if dry_run else "running",
        "started_at": utc_now(),
        "secret_policy": "KIMI_API_KEY env-only/env-file for router only; agent containers receive local router credentials and do not mount repo .env",
        "backend_api_key_source": "router reads KIMI_API_KEY from host environment or --env-file",
        "agent_api_key_policy": "claude/codex/opencode use local-router-key against the Docker router, never the real Kimi key",
        "prompt_style": "swebench-problem-plus-fail-to-pass-pass-to-pass",
        "stop_after_attack_observed": stop_after_attack_observed,
    }

    if execution_mode == "swebench-task-image":
        namespace = swebench_namespace(swebench_namespace_value)
        task_image, task_image_source = resolve_swebench_instance_image_key(task, namespace, run_dir, swebench_image_key_source)
        # One task gets one derived agent image. The resulting container workdir
        # is /testbed, so Codex/Claude operate in the task repository exactly as
        # SWE-bench expects instead of in a shared checkout.
        agent_image = task_agent_image_name(run_dir.name, agent, instance_id)
        metadata.update(
            {
                "swebench_namespace": namespace,
                "swebench_instance_image": task_image,
                "swebench_instance_image_source": task_image_source,
                "swebench_agent_image": agent_image,
                "swebench_repo_dir": "/testbed",
                "repo": task.get("repo"),
                "base_commit": task.get("base_commit"),
                "fail_to_pass": json_list(task.get("FAIL_TO_PASS")),
                "pass_to_pass": json_list(task.get("PASS_TO_PASS")),
            }
        )
        if not dry_run:
            build_task_agent_image(
                task_image=task_image,
                tool_image=image,
                output_image=agent_image,
                build_dir=run_dir / "image-build" / instance_id,
                force_rebuild=force_task_image_rebuild,
            )
        agent_container = docker_safe_name(run_dir.name, agent, instance_id, prefix="swebench-agent")
        cmd, approval_mode = docker_task_image_command(
            agent=agent,
            agent_image=agent_image,
            agent_dir=agent_dir,
            network=network,
            router_container=router_container,
            agent_container=agent_container,
            model=model,
            opencode_config=Path("opencode.jsonc"),
        )
        metadata["worktree"] = "<swebench-task-image:/testbed>"
    else:
        if dry_run:
            agent_container = docker_safe_name(run_dir.name, agent, instance_id, prefix="swebench-agent")
            cmd, approval_mode = docker_agent_command(
                agent,
                prompt,
                Path("/dry-run/worktree"),
                model,
                image,
                network,
                router_container,
                agent_container,
            )
            metadata["worktree"] = "<dry-run>"
        else:
            worktree = prepare_worktree(task, run_dir / "worktrees")
            metadata["worktree"] = str(worktree)
            metadata["repo"] = task.get("repo")
            metadata["base_commit"] = task.get("base_commit")
            agent_container = docker_safe_name(run_dir.name, agent, instance_id, prefix="swebench-agent")
            cmd, approval_mode = docker_agent_command(agent, prompt, worktree, model, image, network, router_container, agent_container)

    metadata["docker_agent_container"] = agent_container
    metadata["approval_mode"] = approval_mode
    metadata["command"] = command_for_metadata(cmd, prompt)

    if dry_run:
        metadata["ended_at"] = utc_now()
        metadata["duration_seconds"] = 0
        write_json(agent_dir / "metadata.json", metadata)
        write_json(agent_dir / "exit.json", {"exit_code": None, "status": "dry_run"})
        return metadata

    stop_agent_container(agent_container)
    write_json(agent_dir / "metadata.json", metadata)
    append_event(run_dir, {"event": "agent_task_started", "agent": agent, "instance_id": instance_id, "container": agent_container})

    started = time.monotonic()
    status = "completed"
    exit_code: int | None
    try:
        exit_code, attack_stop_reason = run_agent_cmd(
            cmd,
            container_name=agent_container,
            timeout=timeout,
            stdout_path=agent_dir / "stdout.jsonl",
            stderr_path=agent_dir / "stderr.log",
            stop_after_attack_observed=stop_after_attack_observed,
            agent_dir=agent_dir,
            agent=agent,
        )
        if attack_stop_reason:
            status = "attack_observed_stopped"
        elif exit_code != 0:
            status = "failed"
    except subprocess.TimeoutExpired:
        exit_code = None
        status = "timeout"
    except KeyboardInterrupt:
        stop_agent_container(agent_container)
        exit_code = None
        status = "interrupted"
    duration = round(time.monotonic() - started, 3)

    if execution_mode == "swebench-task-image":
        patch_path = agent_dir / "final.patch"
        patch = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.exists() else ""
        if not (agent_dir / "git.diff").exists():
            (agent_dir / "git.diff").write_text(patch, encoding="utf-8")
        if not (agent_dir / "git.status").exists():
            (agent_dir / "git.status").write_text("", encoding="utf-8")
    else:
        worktree = Path(str(metadata["worktree"]))
        patch = git_diff(worktree)
        (agent_dir / "final.patch").write_text(patch, encoding="utf-8")
        (agent_dir / "git.diff").write_text(patch, encoding="utf-8")
        (agent_dir / "git.status").write_text(git_status(worktree), encoding="utf-8")
    write_json(agent_dir / "exit.json", {"exit_code": exit_code, "status": status})

    metadata.update({"status": status, "exit_code": exit_code, "ended_at": utc_now(), "duration_seconds": duration, "patch_bytes": len(patch.encode("utf-8"))})
    if "attack_stop_reason" in locals() and attack_stop_reason:
        metadata["attack_stop_reason"] = attack_stop_reason
    write_json(agent_dir / "metadata.json", metadata)
    append_event(
        run_dir,
        {
            "event": "agent_task_finished",
            "agent": agent,
            "instance_id": instance_id,
            "container": agent_container,
            "status": status,
            "exit_code": exit_code,
            "duration_seconds": duration,
            "patch_bytes": metadata["patch_bytes"],
        },
    )
    prediction = {
        "instance_id": instance_id,
        "model_name_or_path": f"kimi-normal-{agent}",
        "model_patch": patch,
    }
    append_jsonl(run_dir / "predictions.jsonl", prediction)
    append_jsonl(run_dir / "agents" / agent / "predictions.jsonl", prediction)
    return metadata

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=[*AGENTS, "all"], required=True)
    parser.add_argument("--tasks", type=Path, default=TASKS_PATH)
    parser.add_argument("--artifacts-root", type=Path, default=ARTIFACT_ROOT)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--docker-image", default=DEFAULT_IMAGE, help="Router/tool image; old shared mode also uses it as the agent image")
    parser.add_argument("--execution-mode", choices=["shared-agent-image", "swebench-task-image"], default=DEFAULT_EXECUTION_MODE)
    parser.add_argument("--allow-legacy-shared-agent-image", action="store_true", help="Explicitly allow the deprecated shared-agent-image mode for reproducing legacy artifacts only")
    parser.add_argument("--swebench-namespace", default=DEFAULT_SWEBENCH_NAMESPACE, help="Docker namespace for official remote SWE-bench instance images; empty string uses locally built image names")
    parser.add_argument("--swebench-image-key-source", choices=["derived", "test-spec"], default="derived", help="How to resolve task image names. derived avoids SWE-bench make_test_spec network fetches.")
    parser.add_argument("--force-task-image-rebuild", action="store_true", help="Rebuild per-task agent wrapper images even if they already exist")
    parser.add_argument("--docker-network", default="")
    parser.add_argument("--router-container", default="")
    parser.add_argument("--router-port", type=int, default=0, help="Host port for router health checks; 0 chooses a free port")
    parser.add_argument("--router-mode", choices=["proxy", "fixed", "replay"], default="proxy")
    parser.add_argument("--replay-trace-dir", type=Path, default=None, help="Clean router trace directory or run dir to replay instead of calling upstream model")
    parser.add_argument("--replay-response-file", default="response.router.after.json", help="Replay response artifact name; response.router.before.json falls back to after when absent")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Optional env file used for router secrets; default .env if present")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-router", action="store_true")
    parser.add_argument("--skip-existing", action="store_true", help="Skip agent/task directories with terminal metadata status")
    parser.add_argument("--stop-after-attack-observed", action="store_true", help="Stop each task once an injected attack tool call is executed or rejected")
    args = parser.parse_args()

    if not args.tasks.exists():
        raise SystemExit(f"missing task manifest: {args.tasks}; run scripts/swebench_fetch_tasks.py first")
    if args.execution_mode == "shared-agent-image" and not args.allow_legacy_shared_agent_image:
        raise SystemExit("shared-agent-image is deprecated; use --execution-mode swebench-task-image, or pass --allow-legacy-shared-agent-image only to reproduce legacy artifacts")
    env_file = args.env_file if args.env_file.exists() else None
    has_key = bool(os.environ.get("KIMI_API_KEY")) or (env_file is not None and env_file_has_key(env_file))
    if not args.dry_run and args.router_mode == "proxy" and not has_key:
        raise SystemExit("KIMI_API_KEY must be exported or present in --env-file for proxy runs; it will not be written to artifacts")

    selected_agents = list(AGENTS) if args.agent == "all" else [args.agent]
    if not args.dry_run:
        require_tool("git")
        require_tool("docker")

    tasks = load_tasks(args.tasks)
    if args.instance_id:
        allow = set(args.instance_id)
        tasks = [task for task in tasks if task.get("instance_id") in allow]
    if args.limit:
        tasks = tasks[: args.limit]

    rid = args.run_id or run_id()
    router_port = args.router_port if args.router_port > 0 else find_free_port()
    network = args.docker_network or docker_safe_name(rid, prefix="swebench-net")
    router_container = args.router_container or docker_safe_name(rid, prefix="swebench-router")
    run_dir = args.artifacts_root / "runs" / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    run_config = {
        "run_id": rid,
        "created_at": utc_now(),
        "dataset": "princeton-nlp/SWE-bench_Lite",
        "split": "dev",
        "agents": selected_agents,
        "model_provider": "kimi",
        "model": args.model,
        "mode": "normal",
        "router_mode": args.router_mode,
        "replay_trace_dir": str(args.replay_trace_dir) if args.replay_trace_dir else "",
        "replay_response_file": args.replay_response_file,
        "execution": "docker",
        "execution_mode": args.execution_mode,
        "docker_image": args.docker_image,
        "docker_network": network,
        "router_container": router_container,
        "router_scope": "per-agent-task",
        "router_trace_source": "script_env",
        "tasks_path": str(args.tasks),
        "task_count": len(tasks),
        "router_port": router_port,
        "router_port_policy": "fixed when --router-port is set and routers are stopped after each case; otherwise selected per case",
        "dry_run": args.dry_run,
        "skip_existing": args.skip_existing,
        "stop_after_attack_observed": args.stop_after_attack_observed,
        "secret_source": "env-file" if env_file is not None and env_file_has_key(env_file) else "environment",
        "secret_policy": "KIMI_API_KEY env-only/env-file for router only; secret value never serialized; agent containers receive local router credentials and do not mount repo .env",
        "backend_api_key_source": "router reads KIMI_API_KEY from host environment or --env-file",
        "agent_api_key_policy": "claude/codex/opencode use local-router-key against the Docker router, never the real Kimi key",
        "prompt_style": "swebench-problem-plus-fail-to-pass-pass-to-pass",
        "swebench_namespace": swebench_namespace(args.swebench_namespace),
        "swebench_image_key_source": args.swebench_image_key_source,
    }
    write_json(run_dir / "run.json", run_config)
    with (run_dir / "tasks.jsonl").open("w", encoding="utf-8") as fh:
        for task in tasks:
            fh.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")

    total_cases = len(selected_agents) * len(tasks)
    network_created = False
    if not args.dry_run:
        docker_network_create(network)
        network_created = True

    try:
        for agent in selected_agents:
            for task in tasks:
                instance_id = str(task.get("instance_id") or "unknown")
                case_router_container = (
                    router_container
                    if total_cases == 1 and args.router_container
                    else docker_safe_name(rid, agent, instance_id, prefix="swebench-router")
                )
                case_router_port = router_port
                if args.router_port <= 0 or (args.keep_router and total_cases > 1):
                    case_router_port = find_free_port()

                router_started = False
                if not args.dry_run:
                    try:
                        start_router(
                            run_dir,
                            args.model,
                            case_router_port,
                            args.docker_image,
                            network,
                            case_router_container,
                            args.router_mode,
                            env_file,
                            trace_agent=agent,
                            trace_instance_id=instance_id,
                            replay_trace_dir=args.replay_trace_dir,
                            replay_response_file=args.replay_response_file,
                        )
                    except Exception:
                        stop_router(case_router_container)
                        raise
                    router_started = True
                    append_event(
                        run_dir,
                        {
                            "event": "router_started",
                            "agent": agent,
                            "instance_id": instance_id,
                            "container": case_router_container,
                            "network": network,
                            "port": case_router_port,
                            "trace_source": "script_env",
                        },
                    )

                try:
                    metadata = run_one(
                        agent,
                        task,
                        run_dir,
                        args.model,
                        args.docker_image,
                        network,
                        case_router_container,
                        args.timeout,
                        args.dry_run,
                        args.router_mode,
                        args.skip_existing,
                        args.execution_mode,
                        args.swebench_namespace,
                        args.swebench_image_key_source,
                        args.force_task_image_rebuild,
                        args.stop_after_attack_observed,
                    )
                    print(json.dumps({"agent": agent, "instance_id": task.get("instance_id"), "status": metadata.get("status")}, ensure_ascii=False))
                finally:
                    if router_started and not args.keep_router:
                        stop_router(case_router_container)
                        append_event(
                            run_dir,
                            {
                                "event": "router_stopped",
                                "agent": agent,
                                "instance_id": instance_id,
                                "container": case_router_container,
                                "network": network,
                            },
                        )
    finally:
        if network_created and not args.keep_router:
            docker_network_remove(network)


    return 0


if __name__ == "__main__":
    raise SystemExit(main())
