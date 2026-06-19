"""Shared router process lifecycle helpers."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RouterProcessSpec:
    host: str
    port: int
    registry_path: Path
    log_path: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class SharedRouterLauncher:
    # manager 只负责启动共享 router；每个 context 的实验语义都来自 registry 文件。
    def build_command(self, spec: RouterProcessSpec) -> list[str]:
        return [
            "python3",
            "-m",
            "uvicorn",
            "router.app:app",
            "--host",
            spec.host,
            "--port",
            str(spec.port),
        ]

    def build_env(self, spec: RouterProcessSpec) -> dict[str, str]:
        env = os.environ.copy()
        env.update(spec.env)
        env["ROUTER_CONTEXT_REGISTRY"] = str(spec.registry_path)
        return env

    def start(self, spec: RouterProcessSpec) -> subprocess.Popen[bytes]:
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = spec.log_path.open("ab")
        return subprocess.Popen(
            self.build_command(spec),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=self.build_env(spec),
        )
