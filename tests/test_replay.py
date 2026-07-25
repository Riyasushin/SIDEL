from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from router.context import ExperimentContext
from router.replay import ReplayLookupError, load_replay_response


def context(trace_source_dir: str, *, replay_match_mode: str = "exact_request_hash") -> ExperimentContext:
    return ExperimentContext(
        run_id="run",
        context_id="ctx",
        api_key="rk",
        agent="claude",
        agent_mode="auto_review",
        instance_id="task",
        router_mode="replay-inject",
        trace_dir=trace_source_dir,
        trace_source_dir=trace_source_dir,
        replay_until_turn=0,
        injection_turn=0,
        dataset_sample_id="sample",
        injection_mode="append",
        payload_execution_allowed=True,
        timeout_seconds=60,
        replay_strict=True,
        replay_response_file="response.router.after_injection.json",
        replay_match_mode=replay_match_mode,  # type: ignore[arg-type]
    )


def write_turn(
    root: Path,
    name: str,
    *,
    request: dict,
    response: dict,
    metadata: dict,
) -> None:
    turn_dir = root / name
    turn_dir.mkdir(parents=True, exist_ok=True)
    (turn_dir / "request.agent.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (turn_dir / "response.router.after_injection.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (turn_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ReplayTest(unittest.TestCase):
    def test_exact_request_hash_mode_keeps_strict_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            request = {"model": "m", "messages": [{"role": "user", "content": "fix"}], "tools": [{"name": "Bash"}]}
            write_turn(
                source_root,
                "0000-anthropic.messages",
                request=request,
                response={"id": "resp-0", "content": [{"type": "text", "text": "ok"}]},
                metadata={"turn_index": 0, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
            )
            replayed, replay = load_replay_response(
                context(tmp, replay_match_mode="exact_request_hash"),
                {"turn_index": 0, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
                request,
            )
            self.assertEqual(replayed["id"], "resp-0")
            self.assertEqual(replay["match_mode"], "exact_request_hash")
            self.assertTrue(replay["request_hash_matched"])

            with self.assertRaisesRegex(ReplayLookupError, "strict replay request hash mismatch"):
                load_replay_response(
                    context(tmp, replay_match_mode="exact_request_hash"),
                    {"turn_index": 0, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
                    {"model": "m", "messages": [{"role": "user", "content": "different"}], "tools": [{"name": "Bash"}]},
                )

    def test_task_turn_compatible_replays_across_title_and_tool_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            write_turn(
                source_root,
                "0000-anthropic.messages",
                request={"model": "m", "messages": [{"role": "user", "content": "Generate a concise, sentence-case title"}], "tools": []},
                response={"id": "title", "content": [{"type": "text", "text": "sqlfluff title"}]},
                metadata={"turn_index": 0, "task_turn_index": None, "request_role": "agent_main", "endpoint": "anthropic.messages"},
            )
            write_turn(
                source_root,
                "0001-anthropic.messages",
                request={"model": "m", "system": "old", "messages": [{"role": "user", "content": "fix bug"}], "tools": [{"name": "Bash"}]},
                response={
                    "id": "task",
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}],
                    "stop_reason": "tool_use",
                },
                metadata={"turn_index": 1, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
            )
            replayed, replay = load_replay_response(
                context(tmp, replay_match_mode="task_turn_compatible"),
                {"turn_index": 0, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
                {
                    "model": "m",
                    "system": "new system prompt with extra guidance",
                    "messages": [{"role": "user", "content": "fix bug"}],
                    "tools": [{"name": "Bash"}, {"name": "Monitor"}],
                },
            )
            self.assertEqual(replayed["id"], "task")
            self.assertEqual(replay["matched_by"], "task_turn_index")
            self.assertEqual(replay["matched_source_turn_index"], 1)
            self.assertFalse(replay["request_hash_matched"])
            self.assertTrue(replay["compatibility_passed"])
            self.assertEqual(replay["source_response_tool_names"], ["Bash"])

    def test_task_turn_compatible_ignores_reviewer_turns_when_aligning_main_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            write_turn(
                source_root,
                "0001-anthropic.messages",
                request={"model": "m", "messages": [{"role": "user", "content": "step0"}], "tools": [{"name": "Bash"}]},
                response={"id": "task-0", "content": [{"type": "text", "text": "step0"}]},
                metadata={"turn_index": 1, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
            )
            write_turn(
                source_root,
                "0001-reviewer-anthropic.messages",
                request={"model": "review", "messages": [{"role": "user", "content": "<transcript/>"}]},
                response={"id": "review-0", "content": [{"type": "text", "text": "<block>no</block>"}]},
                metadata={"turn_index": 1, "task_turn_index": None, "request_role": "reviewer", "endpoint": "anthropic.messages"},
            )
            write_turn(
                source_root,
                "0002-anthropic.messages",
                request={"model": "m", "messages": [{"role": "user", "content": "step1"}], "tools": [{"name": "Bash"}]},
                response={"id": "task-1", "content": [{"type": "text", "text": "step1"}]},
                metadata={"turn_index": 2, "task_turn_index": 1, "request_role": "agent_main", "endpoint": "anthropic.messages"},
            )
            replayed, replay = load_replay_response(
                context(tmp, replay_match_mode="task_turn_compatible"),
                {"turn_index": 1, "task_turn_index": 1, "request_role": "agent_main", "endpoint": "anthropic.messages"},
                {"model": "m", "messages": [{"role": "user", "content": "step1"}], "tools": [{"name": "Bash"}, {"name": "Monitor"}]},
            )
            self.assertEqual(replayed["id"], "task-1")
            self.assertEqual(replay["matched_source_turn_index"], 2)

    def test_task_turn_compatible_fails_when_source_response_tool_is_missing_live_request_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            write_turn(
                source_root,
                "0000-anthropic.messages",
                request={"model": "m", "messages": [{"role": "user", "content": "fix"}], "tools": [{"name": "Bash"}]},
                response={
                    "id": "resp",
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}],
                    "stop_reason": "tool_use",
                },
                metadata={"turn_index": 0, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
            )
            with self.assertRaisesRegex(ReplayLookupError, "task-turn compatible replay incompatible tools"):
                load_replay_response(
                    context(tmp, replay_match_mode="task_turn_compatible"),
                    {"turn_index": 0, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
                    {"model": "m", "messages": [{"role": "user", "content": "fix"}], "tools": [{"name": "Read"}]},
                )

    def test_task_turn_compatible_allows_text_only_response_when_request_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            write_turn(
                source_root,
                "0002-openai.responses",
                request={"model": "m", "input": "fix bug", "tools": [{"type": "function", "function": {"name": "shell"}}]},
                response={"id": "resp", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]},
                metadata={"turn_index": 2, "task_turn_index": 1, "request_role": "agent_main", "endpoint": "openai.responses"},
            )
            replayed, replay = load_replay_response(
                ExperimentContext(
                    run_id="run",
                    context_id="ctx",
                    api_key="rk",
                    agent="codex",
                    agent_mode="auto_review",
                    instance_id="task",
                    router_mode="replay",
                    trace_dir=tmp,
                    trace_source_dir=tmp,
                    replay_until_turn=1,
                    injection_turn=None,
                    dataset_sample_id="",
                    injection_mode="append",
                    payload_execution_allowed=True,
                    timeout_seconds=60,
                    replay_strict=True,
                    replay_response_file="response.router.after_injection.json",
                    replay_match_mode="task_turn_compatible",
                ),
                {"turn_index": 1, "task_turn_index": 1, "request_role": "agent_main", "endpoint": "openai.responses"},
                {"model": "m", "input": "fix bug", "tools": [{"type": "function", "function": {"name": "shell"}}, {"type": "function", "function": {"name": "notify"}}]},
            )
            self.assertEqual(replayed["id"], "resp")
            self.assertTrue(replay["compatibility_passed"])
            self.assertEqual(replay["source_response_tool_names"], [])

    def test_task_turn_compatible_reports_missing_source_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            write_turn(
                source_root,
                "0000-anthropic.messages",
                request={"model": "m", "messages": [{"role": "user", "content": "fix"}], "tools": [{"name": "Bash"}]},
                response={"id": "resp", "content": [{"type": "text", "text": "ok"}]},
                metadata={"turn_index": 0, "task_turn_index": 0, "request_role": "agent_main", "endpoint": "anthropic.messages"},
            )
            with self.assertRaisesRegex(ReplayLookupError, "task-turn compatible replay missing source turn"):
                load_replay_response(
                    context(tmp, replay_match_mode="task_turn_compatible"),
                    {"turn_index": 3, "task_turn_index": 2, "request_role": "agent_main", "endpoint": "anthropic.messages"},
                    {"model": "m", "messages": [{"role": "user", "content": "later"}], "tools": [{"name": "Bash"}]},
                )


if __name__ == "__main__":
    unittest.main()
