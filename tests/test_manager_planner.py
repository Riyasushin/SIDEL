from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from manager.manifest import write_manifests
from manager.planner import MatrixPlanOptions, build_matrix_plan, load_dataset_sample_ids, load_task_instance_ids, parse_turns


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
                replay_match_mode="task_turn_compatible",
                enable_tool_defense=True,
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
            self.assertEqual(manifest_payload["replay_match_mode"], "task_turn_compatible")
            self.assertTrue(manifest_payload["enable_tool_defense"])
            self.assertEqual(registry_payload["contexts"][raw_key]["replay_match_mode"], "task_turn_compatible")
            self.assertTrue(registry_payload["contexts"][raw_key]["enable_tool_defense"])

    def test_load_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = root / "tasks.jsonl"
            tasks.write_text('{"instance_id":"a"}\n{"instance_id":"b"}\n', encoding="utf-8")
            dataset = root / "dataset.jsonl"
            dataset.write_text('{"id":"s1"}\n{"id":"s2"}\n', encoding="utf-8")
            self.assertEqual(load_task_instance_ids(tasks, limit=1), ("a",))
            self.assertEqual(load_dataset_sample_ids(dataset, "1:2"), ("s1", "s2"))
            self.assertEqual(parse_turns("0,2:4", "replay-inject"), (0, 2, 3, 4))

    def test_inject_mode_keeps_injection_turn_without_replay_until_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = MatrixPlanOptions(
                run_id="run",
                router_mode="inject",
                agents=("codex",),
                agent_mode="auto_review",
                instance_ids=("sqlfluff__sqlfluff-1625",),
                dataset_sample_ids=("malexec-001",),
                injection_turns=(5,),
                injection_mode="append",
                payload_execution_allowed=True,
                timeout_seconds=60,
            )
            keyed = build_matrix_plan(options)
            manifest = next(iter(keyed.values()))
            registry = write_manifests(root, keyed, "inject")
            registry_payload = json.loads(registry.read_text())
            raw_key = next(iter(registry_payload["contexts"]))

            self.assertEqual(manifest.router_mode, "inject")
            self.assertIsNone(manifest.replay_until_turn)
            self.assertEqual(manifest.injection_turn, 5)
            self.assertEqual(manifest.dataset_sample_id, "malexec-001")
            self.assertEqual(registry_payload["contexts"][raw_key]["router_mode"], "inject")
            self.assertIsNone(registry_payload["contexts"][raw_key]["replay_until_turn"])
            self.assertEqual(registry_payload["contexts"][raw_key]["injection_turn"], 5)

    def test_random_injection_mode_writes_candidates_without_expanding_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = MatrixPlanOptions(
                run_id="run",
                router_mode="replay-inject",
                agents=("codex",),
                agent_mode="auto_review",
                instance_ids=("sqlfluff__sqlfluff-1625",),
                dataset_sample_ids=("malexec-001",),
                injection_turns=(1, 3, 5),
                injection_mode="append",
                payload_execution_allowed=True,
                timeout_seconds=60,
                trace_source_root=root / "records",
                injection_turn_mode="random",
                injection_random_seed=7,
            )
            keyed = build_matrix_plan(options)
            self.assertEqual(len(keyed), 1)

            manifest = next(iter(keyed.values()))
            registry = write_manifests(root, keyed, "replay-inject")
            registry_payload = json.loads(registry.read_text())
            raw_key = next(iter(registry_payload["contexts"]))

            self.assertIsNone(manifest.injection_turn)
            self.assertEqual(manifest.injection_turn_mode, "random")
            self.assertEqual(manifest.injection_turns, (1, 3, 5))
            self.assertEqual(manifest.injection_random_seed, 7)
            self.assertEqual(manifest.context_id, "run-sqlfluff__sqlfluff-1625-codex-auto_review-malexec-001-turn-random")
            self.assertEqual(registry_payload["contexts"][raw_key]["injection_turn_mode"], "random")
            self.assertEqual(registry_payload["contexts"][raw_key]["injection_turns"], [1, 3, 5])
            self.assertEqual(registry_payload["contexts"][raw_key]["injection_random_seed"], 7)


if __name__ == "__main__":
    unittest.main()
