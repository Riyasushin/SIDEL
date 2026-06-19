from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from router.request_lifecycle import begin_replay_turn, is_auxiliary_endpoint, is_replay_turn_endpoint, stop_if_context_terminal
from router.context import ExperimentContext
from router.tracing import extract_trace_metadata, reset_turn_counters, turn_artifact_dir
from router.session_state import mark_injection_completed, reset_terminal_states, terminal_state_for


def context(trace_dir: str) -> ExperimentContext:
    return ExperimentContext(
        run_id="run",
        context_id="ctx",
        api_key="rk",
        agent="codex",
        agent_mode="auto_review",
        instance_id="task",
        router_mode="replay-inject",
        trace_dir=trace_dir,
        trace_source_dir=trace_dir,
        replay_until_turn=0,
        injection_turn=0,
        dataset_sample_id="sample",
        injection_mode="append",
        payload_execution_allowed=True,
        timeout_seconds=60,
        replay_strict=True,
        replay_response_file="response.router.after_injection.json",
    )


class TerminalStateTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_terminal_states()
        reset_turn_counters()


    def test_endpoint_classification_keeps_auxiliary_requests_out_of_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp)
            self.assertTrue(is_replay_turn_endpoint("openai.responses"))
            self.assertTrue(is_replay_turn_endpoint("anthropic.messages"))
            self.assertFalse(is_replay_turn_endpoint("anthropic.count_tokens"))
            self.assertTrue(is_auxiliary_endpoint("anthropic.count_tokens"))
            self.assertTrue(is_auxiliary_endpoint("openai.models"))
            with self.assertRaisesRegex(ValueError, "does not consume replay turns"):
                begin_replay_turn("anthropic.count_tokens", {"model": "m"}, ctx)


    def test_reviewer_request_bypasses_terminal_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp)
            mark_injection_completed(
                ctx,
                {"turn_index": 3, "endpoint": "openai.responses", "request_role": "agent_main"},
                {"dataset_sample_id": "sample", "injection_turn": 3, "tool_name": "shell", "protocol": "openai.responses"},
            )

            reviewer_metadata = {"turn_index": 0, "endpoint": "openai.responses", "request_role": "reviewer"}
            stop_if_context_terminal(ctx, reviewer_metadata)

            bypass_path = Path(tmp) / "0000-reviewer-openai.responses" / "terminal-bypass.json"
            self.assertTrue(bypass_path.exists())

    def test_reviewer_turns_do_not_advance_main_replay_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp)
            reviewer = extract_trace_metadata({"model": "auto-review-model"}, ctx, "openai.responses")
            main = extract_trace_metadata({"model": "coding-model"}, ctx, "openai.responses")

            self.assertEqual(reviewer["request_role"], "reviewer")
            self.assertEqual(reviewer["turn_index"], 0)
            self.assertEqual(main["request_role"], "agent_main")
            self.assertEqual(main["turn_index"], 0)
            self.assertEqual(
                turn_artifact_dir(ctx, reviewer),
                Path(tmp) / "0000-reviewer-openai.responses",
            )
            self.assertEqual(
                turn_artifact_dir(ctx, main),
                Path(tmp) / "0000-openai.responses",
            )

    def test_terminal_request_is_persisted_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp)
            mark_injection_completed(
                ctx,
                {"turn_index": 0, "endpoint": "openai.responses", "request_role": "agent_main"},
                {"dataset_sample_id": "sample", "injection_turn": 0, "tool_name": "shell", "protocol": "openai.responses"},
            )

            with self.assertRaises(HTTPException):
                begin_replay_turn("openai.responses", {"model": "coding-model", "input": "tool output"}, ctx)

            request_path = Path(tmp) / "0000-openai.responses" / "request.agent.json"
            terminal_path = Path(tmp) / "0000-openai.responses" / "terminal.json"
            self.assertTrue(request_path.exists())
            self.assertTrue(terminal_path.exists())


    def test_next_main_request_after_injection_stops_after_request_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp)
            injected_metadata = {"turn_index": 3, "endpoint": "openai.responses", "request_role": "agent_main"}
            state = mark_injection_completed(
                ctx,
                injected_metadata,
                {"dataset_sample_id": "sample", "injection_turn": 3, "tool_name": "shell", "protocol": "openai.responses"},
            )
            self.assertEqual(terminal_state_for(ctx), state)

            next_metadata = {"turn_index": 4, "endpoint": "openai.responses", "request_role": "agent_main"}
            with self.assertRaises(HTTPException) as raised:
                stop_if_context_terminal(ctx, next_metadata)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(raised.exception.detail["reason"], "injection_completed")
            terminal_path = Path(tmp) / "0004-openai.responses" / "terminal.json"
            self.assertTrue(terminal_path.exists())


    def test_task_turn_starts_at_user_prompt_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = replace(context(tmp), agent="claude")
            title_request = extract_trace_metadata(
                {
                    "model": "m",
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "<session>task</session>"}]}],
                    "output_config": {"format": {"schema": {"properties": {"title": {"type": "string"}}}}},
                    "tools": [],
                },
                ctx,
                "anthropic.messages",
            )
            user_prompt = extract_trace_metadata(
                {
                    "model": "m",
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "Fix the bug"}]}],
                    "tools": [{"name": "Bash"}],
                },
                ctx,
                "anthropic.messages",
            )
            followup = extract_trace_metadata(
                {"model": "m", "messages": [{"role": "user", "content": "tool result"}], "tools": [{"name": "Bash"}]},
                ctx,
                "anthropic.messages",
            )

            self.assertIsNone(title_request["task_turn_index"])
            self.assertEqual(user_prompt["task_turn_index"], 0)
            self.assertEqual(followup["task_turn_index"], 1)
            self.assertEqual(user_prompt["injectable_turn_index"], 0)


if __name__ == "__main__":
    unittest.main()
