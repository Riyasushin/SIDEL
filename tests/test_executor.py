from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from manager.cli import main as cli_main
from manager.executor import ExperimentExecutor
from manager.orchestrator import ExperimentOrchestrator


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


if __name__ == "__main__":
    unittest.main()
