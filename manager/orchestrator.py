"""Bridge manager manifests to SWE-bench container run specs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Collection

from manager.manifest import ExperimentManifest
from swebench_container.models import ContainerRunSpec


class ExperimentOrchestrator:
    # prompt 只来自 SWE-bench task；router mode/sample/turn 只能来自 manifest/registry。
    def __init__(self, *, run_root: Path, router_url: str, tasks_path: Path, prompt_template_path: Path) -> None:
        self.run_root = run_root
        self.router_url = router_url
        self.tasks = load_tasks(tasks_path)
        self.prompt_template = prompt_template_path.read_text(encoding="utf-8")
        self.api_keys_by_context = load_api_keys_by_context(run_root / "router" / "effective_config.json")

    def build_container_specs(self, context_ids: Collection[str] | None = None) -> list[ContainerRunSpec]:
        selected_context_ids = set(context_ids) if context_ids is not None else None
        specs: list[ContainerRunSpec] = []
        for manifest_path in sorted((self.run_root / "experiments").glob("*/manifest.json")):
            manifest = ExperimentManifest(**json.loads(manifest_path.read_text(encoding="utf-8")))
            if selected_context_ids is not None and manifest.context_id not in selected_context_ids:
                continue
            specs.append(self.build_container_spec(manifest))
        return specs

    def build_container_spec(self, manifest: ExperimentManifest) -> ContainerRunSpec:
        task = self.tasks.get(manifest.instance_id)
        if task is None:
            raise ValueError(f"missing task row for instance_id={manifest.instance_id}")
        api_key = self.api_keys_by_context.get(manifest.context_id)
        if api_key is None:
            raise ValueError(f"missing router API key for context_id={manifest.context_id}")
        context_root = self.run_root / "experiments" / manifest.context_id
        return ContainerRunSpec(
            run_id=manifest.run_id,
            context_id=manifest.context_id,
            instance_id=manifest.instance_id,
            agent=manifest.agent,
            agent_mode=manifest.agent_mode,
            router_url=self.router_url,
            router_api_key=api_key,
            artifact_dir=context_root / "container",
            swebench_image=manifest.swebench_image,
            wrapper_image=manifest.wrapper_image,
            issue_prompt=render_prompt(self.prompt_template, task),
            timeout_seconds=manifest.timeout_seconds,
            platform=os.environ.get("DOCKER_PLATFORM", "linux/amd64"),
            docker_inspect_path=context_root / "container" / "docker.inspect.json",
        )


def load_api_keys_by_context(registry_path: Path) -> dict[str, str]:
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    contexts = payload.get("contexts")
    if not isinstance(contexts, dict):
        raise ValueError(f"router registry has no contexts object: {registry_path}")
    out: dict[str, str] = {}
    for api_key, context in contexts.items():
        if isinstance(context, dict) and context.get("context_id"):
            out[str(context["context_id"])] = str(api_key)
    return out


def load_tasks(tasks_path: Path) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    with tasks_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and row.get("instance_id"):
                tasks[str(row["instance_id"])] = row
    if not tasks:
        raise ValueError(f"no task rows in {tasks_path}")
    return tasks


def render_prompt(template: str, task: dict[str, Any]) -> str:
    return template.format(
        repo=task.get("repo") or "unknown",
        base_commit=task.get("base_commit") or "unknown",
        instance_id=task.get("instance_id") or "unknown",
        problem_statement=task.get("problem_statement") or "",
        fail_to_pass=task.get("FAIL_TO_PASS") or "[]",
        pass_to_pass=task.get("PASS_TO_PASS") or "[]",
    )
