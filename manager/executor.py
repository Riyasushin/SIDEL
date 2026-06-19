"""Run planned SWE-bench container contexts."""

from __future__ import annotations

import json
from pathlib import Path

from manager.analysis import ContextResult
from manager.orchestrator import ExperimentOrchestrator
from swebench_container.images import WrapperImageBuilder, wrapper_image_spec
from swebench_container.models import ContainerRunResult, ContainerRunSpec
from swebench_container.runner import DockerTaskRunner, redact_command


class ExperimentExecutor:
    def __init__(
        self,
        *,
        orchestrator: ExperimentOrchestrator,
        runner: DockerTaskRunner | None = None,
        image_builder: WrapperImageBuilder | None = None,
        agent_tool_image: str = "code-agent-router:local",
        force_image_rebuild: bool = False,
    ) -> None:
        self.orchestrator = orchestrator
        self.runner = runner or DockerTaskRunner()
        self.image_builder = image_builder or WrapperImageBuilder()
        self.agent_tool_image = agent_tool_image
        self.force_image_rebuild = force_image_rebuild

    def run_all(self, *, dry_run: bool = False, build_images: bool = False) -> list[ContextResult]:
        results: list[ContextResult] = []
        for spec in self.orchestrator.build_container_specs():
            result = self.run_one(spec, dry_run=dry_run, build_images=build_images)
            results.append(result)
        return results

    def run_one(self, spec: ContainerRunSpec, *, dry_run: bool, build_images: bool) -> ContextResult:
        if build_images:
            # 中文注释：镜像构建是独立验证项；dry-run 也允许只检查 wrapper image 能否生成。
            image_result = self.build_wrapper_image(spec)
            if image_result.exit_code != 0:
                result = ContextResult(spec.context_id, "image_build_failed", "review_unknown", "execution_unknown", "wrapper image build failed")
                write_context_result(spec, result)
                return result
        if dry_run:
            write_dry_run(spec, self.runner.build_docker_command(spec))
            result = ContextResult(spec.context_id, "completed", "review_unknown", "execution_unknown", "dry_run")
        else:
            container_result = self.runner.run(spec)
            result = context_result_from_container(container_result)
        write_context_result(spec, result)
        return result

    def build_wrapper_image(self, spec: ContainerRunSpec):
        image_spec = wrapper_image_spec(
            run_root=self.orchestrator.run_root,
            run_id=spec.run_id,
            instance_id=spec.instance_id,
            agent=spec.agent,
            base_image=spec.swebench_image,
            agent_tool_image=self.agent_tool_image,
            force_rebuild=self.force_image_rebuild,
        )
        return self.image_builder.ensure_image(image_spec, artifact_dir=spec.artifact_dir.parent / "image")


def context_result_from_container(result: ContainerRunResult) -> ContextResult:
    status = result.status if result.status in {"completed", "agent_timeout", "agent_crashed", "payload_blocked"} else "analysis_parse_failed"
    return ContextResult(
        context_id=result.context_id,
        status=status,  # type: ignore[arg-type]
        review_outcome="review_unknown",
        execution_outcome="execution_unknown",
        reason=f"container exit_code={result.exit_code}",
    )


def write_context_result(spec: ContainerRunSpec, result: ContextResult) -> None:
    path = spec.artifact_dir.parent / "analysis" / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.__dict__, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_dry_run(spec: ContainerRunSpec, command: list[str]) -> None:
    spec.artifact_dir.mkdir(parents=True, exist_ok=True)
    (spec.artifact_dir / "prompt.md").write_text(spec.issue_prompt, encoding="utf-8")
    (spec.artifact_dir / "dry-run-command.json").write_text(
        json.dumps({"context_id": spec.context_id, "command": redact_command(command)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
