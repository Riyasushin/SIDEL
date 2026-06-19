from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from manager.manifest import write_manifests
from manager.planner import MatrixPlanOptions, build_matrix_plan, load_dataset_sample_ids, load_task_instance_ids


class ManagerPlannerTest(unittest.TestCase):
    def test_builds_context_registry_without_raw_key_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = MatrixPlanOptions(
                run_id="run",
                router_mode="replay-inject",
                agents=("codex",),
                agent_mode="auto_review",
                instance_ids=("sqlfluff__sqlfluff-1625",),
                dataset_sample_ids=("malexec-001",),
                injection_turns=(0,),
                injection_mode="append",
                payload_execution_allowed=True,
                timeout_seconds=60,
                trace_source_root=root / "records",
            )
            keyed = build_matrix_plan(options)
            registry = write_manifests(root, keyed, "replay-inject")
            registry_payload = json.loads(registry.read_text())
            raw_key = next(iter(registry_payload["contexts"]))
            manifest_path = next((root / "run" / "experiments").glob("*/manifest.json"))
            manifest_payload = json.loads(manifest_path.read_text())
            self.assertTrue(raw_key.startswith("rk_codex_"))
            self.assertNotIn(raw_key, manifest_path.read_text())
            self.assertIn("router_api_key_hash", manifest_payload)

    def test_load_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = root / "tasks.jsonl"
            tasks.write_text('{"instance_id":"a"}\n{"instance_id":"b"}\n', encoding="utf-8")
            dataset = root / "dataset.jsonl"
            dataset.write_text('{"id":"s1"}\n{"id":"s2"}\n', encoding="utf-8")
            self.assertEqual(load_task_instance_ids(tasks, limit=1), ("a",))
            self.assertEqual(load_dataset_sample_ids(dataset, "1:2"), ("s1", "s2"))


if __name__ == "__main__":
    unittest.main()
