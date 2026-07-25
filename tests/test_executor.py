from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from manager.cli import main as cli_main
from manager.executor import ExperimentExecutor
from manager.orchestrator import ExperimentOrchestrator
from swebench_container.models import ContainerRunResult, ContainerRunSpec


class ExecutorTest(unittest.TestCase):
    def test_dry_run_writes_command_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_argv = sys.argv
            try:
                sys.argv = [
                    "manager.cli",
                    "plan",
                    "--run-id",
                    "run",
                    "--artifacts-root",
                    str(root),
                    "--task-limit",
                    "1",
                    "--samples",
                    "1",
                    "--agents",
                    "codex",
                    "--router-mode",
                    "proxy-record",
                ]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(), 0)
            finally:
                sys.argv = old_argv

            orchestrator = ExperimentOrchestrator(
                run_root=root / "run",
                router_url="http://router:8000",
                tasks_path=Path("bench/swe_lite_dev_23.jsonl"),
                prompt_template_path=Path("bench/prompts/solve_issue.md"),
            )
            results = ExperimentExecutor(orchestrator=orchestrator).run_all(dry_run=True)
            self.assertEqual(len(results), 1)
            context_id = results[0].context_id
            command_path = root / "run" / "experiments" / context_id / "container" / "dry-run-command.json"
            result_path = root / "run" / "experiments" / context_id / "analysis" / "result.json"
            self.assertTrue(command_path.exists())
            command_text = command_path.read_text()
            self.assertIn("OPENAI_API_KEY=<redacted>", command_text)
            self.assertNotIn("OPENAI_API_KEY=rk_", command_text)
            self.assertTrue((root / "run" / "experiments" / context_id / "container" / "prompt.md").exists())
            self.assertEqual(json.loads(result_path.read_text())["reason"], "dry_run")

    def test_dry_run_can_build_wrapper_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_argv = sys.argv
            try:
                sys.argv = [
                    "manager.cli",
                    "plan",
                    "--run-id",
                    "run",
                    "--artifacts-root",
                    str(root),
                    "--task-limit",
                    "1",
                    "--samples",
                    "1",
                    "--agents",
                    "codex",
                    "--router-mode",
                    "proxy-record",
                ]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(), 0)
            finally:
                sys.argv = old_argv

            class FakeImageBuilder:
                def __init__(self) -> None:
                    self.calls = 0

                def ensure_image(self, spec, *, artifact_dir):
                    self.calls += 1
                    self.artifact_dir = artifact_dir
                    return SimpleNamespace(exit_code=0)

            fake_builder = FakeImageBuilder()
            orchestrator = ExperimentOrchestrator(
                run_root=root / "run",
                router_url="http://router:8000",
                tasks_path=Path("bench/swe_lite_dev_23.jsonl"),
                prompt_template_path=Path("bench/prompts/solve_issue.md"),
            )
            results = ExperimentExecutor(orchestrator=orchestrator, image_builder=fake_builder).run_all(
                dry_run=True,
                build_images=True,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(fake_builder.calls, 1)
            self.assertTrue(str(fake_builder.artifact_dir).endswith("/image"))
            self.assertEqual(results[0].status, "completed")

    def test_parallel_run_preserves_spec_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = [make_spec(root, f"ctx-{index}") for index in range(4)]
            runner = FakeRunner()
            executor = ExperimentExecutor(orchestrator=FakeOrchestrator(root, specs), runner=runner)

            results = executor.run_all(workers=4)

            self.assertEqual([result.context_id for result in results], [spec.context_id for spec in specs])
            self.assertGreaterEqual(runner.max_active, 2)

    def test_workers_one_matches_sequential_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = [make_spec(root, f"ctx-{index}") for index in range(3)]
            runner = FakeRunner()
            executor = ExperimentExecutor(orchestrator=FakeOrchestrator(root, specs), runner=runner)

            results = executor.run_all(workers=1)

            self.assertEqual([result.context_id for result in results], [spec.context_id for spec in specs])
            self.assertEqual(runner.max_active, 1)

    def test_build_images_reuses_wrapper_image_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = "wrapper/shared:latest"
            specs = [
                make_spec(root, "ctx-a", wrapper_image=shared),
                make_spec(root, "ctx-b", wrapper_image=shared),
                make_spec(root, "ctx-c", wrapper_image="wrapper/other:latest"),
            ]
            fake_builder = FakeImageBuilder()
            runner = FakeRunner()
            executor = ExperimentExecutor(orchestrator=FakeOrchestrator(root, specs), runner=runner, image_builder=fake_builder)

            results = executor.run_all(build_images=True, workers=4)

            self.assertEqual(len(results), 3)
            self.assertEqual(fake_builder.calls, [shared, "wrapper/other:latest"])

    def test_failed_image_build_blocks_only_matching_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = [
                make_spec(root, "ctx-a", wrapper_image="wrapper/fail:latest"),
                make_spec(root, "ctx-b", wrapper_image="wrapper/fail:latest"),
                make_spec(root, "ctx-c", wrapper_image="wrapper/ok:latest"),
            ]
            fake_builder = FakeImageBuilder(fail_images={"wrapper/fail:latest"})
            runner = FakeRunner()
            executor = ExperimentExecutor(orchestrator=FakeOrchestrator(root, specs), runner=runner, image_builder=fake_builder)

            results = executor.run_all(build_images=True, workers=4)

            by_context = {result.context_id: result for result in results}
            self.assertEqual(by_context["ctx-a"].status, "image_build_failed")
            self.assertEqual(by_context["ctx-b"].status, "image_build_failed")
            self.assertEqual(by_context["ctx-c"].status, "completed")
            self.assertEqual(runner.calls, ["ctx-c"])

    def test_run_all_rejects_non_positive_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = [make_spec(root, "ctx")]
            executor = ExperimentExecutor(orchestrator=FakeOrchestrator(root, specs), runner=FakeRunner())

            with self.assertRaisesRegex(ValueError, "workers must be > 0"):
                executor.run_all(workers=0)

    def test_cli_run_containers_uses_default_workers(self) -> None:
        captured: dict[str, int] = {}

        class FakeExecutor:
            def __init__(self, **kwargs) -> None:
                pass

            def run_all(self, *, dry_run: bool = False, build_images: bool = False, workers: int = 4):
                captured["workers"] = workers
                return []

        with mock.patch("manager.cli.ExperimentExecutor", FakeExecutor), mock.patch("manager.cli.build_orchestrator", return_value=object()):
            old_argv = sys.argv
            try:
                sys.argv = ["manager.cli", "run-containers", "artifacts/runs/run"]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(), 0)
            finally:
                sys.argv = old_argv

        self.assertEqual(captured["workers"], 4)

    def test_cli_run_containers_accepts_custom_workers(self) -> None:
        captured: dict[str, int] = {}

        class FakeExecutor:
            def __init__(self, **kwargs) -> None:
                pass

            def run_all(self, *, dry_run: bool = False, build_images: bool = False, workers: int = 4):
                captured["workers"] = workers
                return []

        with mock.patch("manager.cli.ExperimentExecutor", FakeExecutor), mock.patch("manager.cli.build_orchestrator", return_value=object()):
            old_argv = sys.argv
            try:
                sys.argv = ["manager.cli", "run-containers", "artifacts/runs/run", "--workers", "6"]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(), 0)
            finally:
                sys.argv = old_argv

        self.assertEqual(captured["workers"], 6)

    def test_cli_rejects_non_positive_workers(self) -> None:
        old_argv = sys.argv
        stderr = io.StringIO()
        try:
            sys.argv = ["manager.cli", "run-containers", "artifacts/runs/run", "--workers", "0"]
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                cli_main()
        finally:
            sys.argv = old_argv

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("must be > 0", stderr.getvalue())


class FakeOrchestrator:
    def __init__(self, run_root: Path, specs: list[ContainerRunSpec]) -> None:
        self.run_root = run_root
        self._specs = specs

    def build_container_specs(self) -> list[ContainerRunSpec]:
        return list(self._specs)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()

    def build_docker_command(self, spec: ContainerRunSpec) -> list[str]:
        return ["docker", "run", spec.context_id]

    def run(self, spec: ContainerRunSpec) -> ContainerRunResult:
        with self._lock:
            self.calls.append(spec.context_id)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(0.05)
        with self._lock:
            self._active -= 1
        return ContainerRunResult(
            context_id=spec.context_id,
            status="completed",
            exit_code=0,
            timed_out=False,
            stdout_path=spec.artifact_dir / "stdout.log",
            stderr_path=spec.artifact_dir / "stderr.log",
            artifact_dir=spec.artifact_dir,
        )


class FakeImageBuilder:
    def __init__(self, *, fail_images: set[str] | None = None) -> None:
        self.fail_images = fail_images or set()
        self.calls: list[str] = []

    def ensure_image(self, spec, *, artifact_dir):
        self.calls.append(spec.wrapper_image)
        return SimpleNamespace(exit_code=1 if spec.wrapper_image in self.fail_images else 0)


def make_spec(root: Path, context_id: str, *, wrapper_image: str = "wrapper:latest") -> ContainerRunSpec:
    return ContainerRunSpec(
        run_id="run",
        context_id=context_id,
        instance_id="task",
        agent="codex",
        agent_mode="auto_review",
        router_url="http://router:8000",
        router_api_key="rk_secret",
        artifact_dir=root / "experiments" / context_id / "container",
        swebench_image="swebench/base",
        wrapper_image=wrapper_image,
        issue_prompt=f"fix {context_id}",
    )


if __name__ == "__main__":
    unittest.main()
