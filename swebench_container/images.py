"""SWE-bench image naming and wrapper-image build lifecycle."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WrapperImageSpec:
    base_image: str
    wrapper_image: str
    agent_tool_image: str
    build_context: Path
    dockerfile_path: Path
    platform: str = "linux/amd64"
    force_rebuild: bool = False

    def build_command(self) -> list[str]:
        return [
            "docker",
            "build",
            "--platform",
            self.platform,
            "--build-arg",
            f"BASE_IMAGE={self.base_image}",
            "--build-arg",
            f"AGENT_TOOL_IMAGE={self.agent_tool_image}",
            "--tag",
            self.wrapper_image,
            "--file",
            str(self.dockerfile_path),
            str(self.build_context),
        ]


@dataclass(frozen=True)
class ImageBuildResult:
    wrapper_image: str
    built: bool
    exit_code: int
    log_path: Path
    metadata_path: Path


class WrapperImageBuilder:
    def ensure_image(self, spec: WrapperImageSpec, *, artifact_dir: Path) -> ImageBuildResult:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = artifact_dir / "image-build.log"
        metadata_path = artifact_dir / "image-build.json"
        write_wrapper_dockerfile(spec.dockerfile_path)
        if not spec.force_rebuild and image_exists(spec.wrapper_image):
            result = ImageBuildResult(spec.wrapper_image, False, 0, log_path, metadata_path)
            write_image_build_metadata(spec, result)
            return result
        command = spec.build_command()
        with log_path.open("wb") as log:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        result = ImageBuildResult(spec.wrapper_image, True, completed.returncode, log_path, metadata_path)
        write_image_build_metadata(spec, result)
        return result


def write_wrapper_dockerfile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """ARG BASE_IMAGE
ARG AGENT_TOOL_IMAGE
FROM ${AGENT_TOOL_IMAGE} AS agent_tools
FROM ${BASE_IMAGE}
COPY --from=agent_tools /usr/local /usr/local
ENV PATH=/usr/local/bin:$PATH
RUN id -u sidel >/dev/null 2>&1 || useradd --create-home --shell /bin/sh sidel
WORKDIR /testbed
""",
        encoding="utf-8",
    )


def image_exists(image: str) -> bool:
    completed = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return completed.returncode == 0


def write_image_build_metadata(spec: WrapperImageSpec, result: ImageBuildResult) -> None:
    result.metadata_path.write_text(
        json.dumps(
            {
                "base_image": spec.base_image,
                "wrapper_image": spec.wrapper_image,
                "agent_tool_image": spec.agent_tool_image,
                "build_context": str(spec.build_context),
                "dockerfile_path": str(spec.dockerfile_path),
                "platform": spec.platform,
                "force_rebuild": spec.force_rebuild,
                "built": result.built,
                "exit_code": result.exit_code,
                "log_path": str(result.log_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def wrapper_image_spec(
    *,
    run_root: Path,
    run_id: str,
    instance_id: str,
    agent: str,
    base_image: str,
    agent_tool_image: str,
    wrapper_image: str | None = None,
    platform: str = "linux/amd64",
    force_rebuild: bool = False,
) -> WrapperImageSpec:
    context = run_root / "wrapper-build" / safe_token(instance_id) / safe_token(agent)
    return WrapperImageSpec(
        base_image=base_image,
        wrapper_image=wrapper_image or default_wrapper_image(run_id, instance_id, agent),
        agent_tool_image=agent_tool_image,
        build_context=context,
        dockerfile_path=context / "Dockerfile",
        platform=platform,
        force_rebuild=force_rebuild,
    )


def candidate_swebench_images(instance_id: str, architecture: str = "x86_64") -> tuple[str, ...]:
    owner_repo_task = instance_id.replace("__", "_1776_").replace("/", "_")
    repo_task = instance_id.replace("__", "_").replace("/", "_")
    preserved = instance_id.replace("/", "_")
    prefix = f"swebench/sweb.eval.{architecture}."
    return dedupe((f"{prefix}{owner_repo_task}", f"{prefix}{repo_task}", f"{prefix}{preserved}"))


def default_swebench_image(instance_id: str, architecture: str = "x86_64") -> str:
    # 中文注释：SWE-bench 官方镜像通常把 owner/repo 分隔编码为 `_1776_`。
    # 先命中本地已有镜像，离线 smoke test 不会被错误的 fallback 标签阻断。
    candidates = candidate_swebench_images(instance_id, architecture)
    for image in candidates:
        if image_exists(image):
            return image
    return candidates[0]


def default_wrapper_image(run_id: str, instance_id: str, agent: str) -> str:
    safe_run = run_id.replace("/", "-").replace(":", "-")
    safe_instance = instance_id.replace("__", "_").replace("/", "_")
    return f"code-agent-attack/{safe_run}/{safe_instance}-{agent}:latest"


def safe_token(value: str) -> str:
    chars = [char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value]
    return "".join(chars).strip("-._") or "unknown"


def dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return tuple(unique)
