from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from router.context import ExperimentContext
from router.injection import inject_tool_call, should_inject


class InjectionTest(unittest.TestCase):
    def test_openai_response_append_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "dataset.jsonl"
            row = {"id": "sample", "tool_name": "shell", "tool_arguments": {"command": "echo owned"}}
            dataset.write_text(json.dumps(row) + "\n", encoding="utf-8")
            context = ExperimentContext(
                run_id="run",
                context_id="ctx",
                api_key="rk",
                agent="codex",
                agent_mode="auto_review",
                instance_id="task",
                router_mode="replay-inject",
                trace_dir=tmp,
                trace_source_dir=tmp,
                replay_until_turn=0,
                injection_turn=0,
                dataset_sample_id="sample",
                injection_mode="append",
                payload_execution_allowed=True,
                timeout_seconds=60,
                replay_strict=True,
                replay_response_file="response.router.after_injection.json",
            )
            metadata = {"turn_index": 0, "task_turn_index": 0}
            self.assertTrue(should_inject(context, metadata))
            response, record = inject_tool_call(
                protocol="openai.responses",
                request_body={"model": "m", "tools": [{"name": "shell"}]},
                response={"id": "resp", "object": "response", "model": "m", "output": []},
                context=context,
                metadata=metadata,
                dataset_path=str(dataset),
                fallback_tool_name="",
                request_id="resp",
                codex_sandbox_permissions="",
                codex_approval_justification="",
            )
            self.assertEqual(response["output"][0]["type"], "function_call")
            self.assertEqual(record["dataset_sample_id"], "sample")


    def test_step_zero_uses_task_turn_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ExperimentContext(
                run_id="run",
                context_id="ctx",
                api_key="rk",
                agent="claude",
                agent_mode="auto_review",
                instance_id="task",
                router_mode="replay-inject",
                trace_dir=tmp,
                trace_source_dir="",
                replay_until_turn=0,
                injection_turn=0,
                dataset_sample_id="sample",
                injection_mode="replace",
                payload_execution_allowed=True,
                timeout_seconds=60,
                replay_strict=True,
                replay_response_file="response.router.after_injection.json",
            )

            self.assertFalse(should_inject(context, {"turn_index": 0, "task_turn_index": None, "request_role": "agent_main"}))
            self.assertTrue(should_inject(context, {"turn_index": 1, "task_turn_index": 0, "request_role": "agent_main"}))



    def test_anthropic_replace_uses_attack_tool_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "dataset.jsonl"
            row = {"id": "sample", "tool_name": "Bash", "tool_arguments": {"command": "echo owned"}}
            dataset.write_text(json.dumps(row) + "\n", encoding="utf-8")
            context = ExperimentContext(
                run_id="run",
                context_id="ctx",
                api_key="rk",
                agent="claude",
                agent_mode="auto_review",
                instance_id="task",
                router_mode="replay-inject",
                trace_dir=tmp,
                trace_source_dir="",
                replay_until_turn=0,
                injection_turn=0,
                dataset_sample_id="sample",
                injection_mode="replace",
                payload_execution_allowed=True,
                timeout_seconds=60,
                replay_strict=True,
                replay_response_file="response.router.after_injection.json",
            )
            response, record = inject_tool_call(
                protocol="anthropic.messages",
                request_body={"model": "m", "tools": [{"name": "Bash"}]},
                response={},
                context=context,
                metadata={"turn_index": 0},
                dataset_path=str(dataset),
                fallback_tool_name="",
                request_id="msg_123456789abc",
                codex_sandbox_permissions="",
                codex_approval_justification="",
            )
            self.assertEqual(response["content"][0]["id"], "toolu_attack_123456789abc")
            self.assertEqual(record["tool_name"], "Bash")


if __name__ == "__main__":
    unittest.main()
