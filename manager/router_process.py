"""Shared router process lifecycle helpers."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


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
            sys.executable,
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
        with spec.log_path.open("ab") as log:
            return subprocess.Popen(
                self.build_command(spec),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=self.build_env(spec),
            )


def wait_for_router(spec: RouterProcessSpec, process: subprocess.Popen[bytes], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    host = "127.0.0.1" if spec.host in {"0.0.0.0", "::"} else spec.host
    url = f"http://{host}:{spec.port}/health"
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"router exited early with code {process.returncode}; see {spec.log_path}")
        try:
            with opener.open(Request(url, method="GET"), timeout=1.0) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            time.sleep(0.2)
    raise TimeoutError(f"router did not become healthy within {timeout}s; see {spec.log_path}")


def stop_router(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
