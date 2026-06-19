from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from manager.cli import main as cli_main
from manager.orchestrator import ExperimentOrchestrator


class OrchestratorTest(unittest.TestCase):
    def test_builds_container_spec_without_router_state_in_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Use the real planner CLI to create the same files a run would use.
            import sys
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
            specs = orchestrator.build_container_specs()
            self.assertEqual(len(specs), 1)
            spec = specs[0]
            self.assertEqual(spec.router_url, "http://router:8000")
            self.assertIn("Problem statement:", spec.issue_prompt)
            self.assertNotIn("dataset_sample_id", spec.issue_prompt)
            self.assertNotIn("injection_turn", spec.issue_prompt)
            self.assertTrue(spec.router_api_key.startswith("rk_"))


if __name__ == "__main__":
    unittest.main()
