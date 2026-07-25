from __future__ import annotations

import unittest

from fastapi import HTTPException

from router.config import Settings
from router.context import ContextRegistry, ExperimentContext, contexts_from_payload


def context(api_key: str, context_id: str) -> ExperimentContext:
    return ExperimentContext(
        run_id="run",
        context_id=context_id,
        api_key=api_key,
        agent="codex",
        agent_mode="auto_review",
        instance_id="task",
        router_mode="replay-inject",
        trace_dir="/tmp/trace",
        trace_source_dir="/tmp/source",
        replay_until_turn=0,
        injection_turn=0,
        dataset_sample_id="sample",
        injection_mode="append",
        payload_execution_allowed=True,
        timeout_seconds=1800,
        replay_strict=True,
        replay_response_file="response.router.after_injection.json",
    )


class ContextRegistryTest(unittest.TestCase):
    def test_resolves_context_by_api_key(self) -> None:
        registry = ContextRegistry([context("rk_one", "ctx-one"), context("rk_two", "ctx-two")])
        self.assertEqual(registry.resolve("rk_two").context_id, "ctx-two")

    def test_rejects_unknown_key_when_multiple_contexts_exist(self) -> None:
        registry = ContextRegistry([context("rk_one", "ctx-one"), context("rk_two", "ctx-two")])
        with self.assertRaises(HTTPException):
            registry.resolve("missing")

    def test_manifest_metadata_does_not_expose_raw_api_key(self) -> None:
        metadata = context("rk_secret", "ctx").public_metadata()
        self.assertNotIn("api_key", metadata)
        self.assertEqual(metadata["router_api_key_hash"], context("rk_secret", "ctx").api_key_hash)
        self.assertEqual(metadata["replay_match_mode"], "exact_request_hash")

    def test_context_registry_preserves_random_injection_fields(self) -> None:
        registry = ContextRegistry(
            [
                ExperimentContext(
                    run_id="run",
                    context_id="ctx-random",
                    api_key="rk_random",
                    agent="codex",
                    agent_mode="auto_review",
                    instance_id="task",
                    router_mode="replay-inject",
                    trace_dir="/tmp/trace",
                    trace_source_dir="/tmp/source",
                    replay_until_turn=None,
                    injection_turn=None,
                    dataset_sample_id="sample",
                    injection_mode="append",
                    payload_execution_allowed=True,
                    timeout_seconds=1800,
                    replay_strict=True,
                    replay_response_file="response.router.after_injection.json",
                    injection_turn_mode="random",
                    injection_turns=(1, 2, 5),
                    injection_random_seed=11,
                )
            ]
        )
        resolved = registry.resolve("rk_random")
        self.assertEqual(resolved.injection_turn_mode, "random")
        self.assertEqual(resolved.injection_turns, (1, 2, 5))
        self.assertEqual(resolved.injection_random_seed, 11)

    def test_contexts_from_payload_preserves_random_injection_fields(self) -> None:
        payload = {
            "run_id": "run",
            "router_mode": "replay-inject",
            "contexts": {
                "rk_random": {
                    "context_id": "ctx-random",
                    "agent": "codex",
                    "agent_mode": "auto_review",
                    "instance_id": "task",
                    "router_mode": "replay-inject",
                    "trace_source_dir": "/tmp/source",
                    "replay_until_turn": None,
                    "injection_turn": None,
                    "injection_turn_mode": "random",
                    "injection_turns": [1, 2, 5],
                    "injection_random_seed": 11,
                    "dataset_sample_id": "sample",
                    "injection_mode": "append",
                    "payload_execution_allowed": True,
                    "timeout_seconds": 1800,
                }
            },
        }
        resolved = contexts_from_payload(payload, Settings())[0]
        self.assertEqual(resolved.injection_turn_mode, "random")
        self.assertEqual(resolved.injection_turns, (1, 2, 5))
        self.assertEqual(resolved.injection_random_seed, 11)


if __name__ == "__main__":
    unittest.main()
