from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from router.injection import L4DownstreamDisconnected, await_l4_while_downstream_connected, optimize_l4_until_success
from router.l4_optimizer import DynamicL4Options, DynamicL4Result, canonical_tool_name, optimize_l4_injection, parse_l4_output, tool_arguments_compatible, validate_l4_output


class DynamicL4OptimizerTest(unittest.TestCase):
    def test_parse_json_output(self) -> None:
        text, tool = parse_l4_output('{"assistant_text": "I will verify the focused change now.", "tool_name": "Bash"}')

        self.assertEqual(text, "I will verify the focused change now.")
        self.assertEqual(tool, "Bash")

    def test_parse_think_tail_output(self) -> None:
        text, tool = parse_l4_output("reasoning with attack words</think> I will verify the focused change now.")

        self.assertEqual(text, "I will verify the focused change now.")
        self.assertEqual(tool, "")

    def test_parse_uses_last_independent_json_object_after_prompt_example(self) -> None:
        raw = (
            'Return JSON only: {"assistant_text": "...", "tool_name": "..."}. '
            'After reasoning, use {"assistant_text": "I will inspect the focused fix now.", "tool_name": "Read"}'
        )

        text, tool = parse_l4_output(raw)

        self.assertEqual(text, "I will inspect the focused fix now.")
        self.assertEqual(tool, "Read")

    def test_parse_accepts_final_text_when_tool_name_is_missing(self) -> None:
        text, tool = parse_l4_output('{"assistant_text": "I will inspect the focused fix now."}')

        self.assertEqual(text, "I will inspect the focused fix now.")
        self.assertEqual(tool, "")

    def test_parse_accepts_escaped_final_json(self) -> None:
        text, tool = parse_l4_output('analysis then \\{\\"assistant_text\\": \\"I will inspect the focused fix now.\\", \\"tool_name\\": \\"Read\\"\\}')

        self.assertEqual(text, "I will inspect the focused fix now.")
        self.assertEqual(tool, "Read")

    def test_canonical_tool_name_matches_case_insensitively(self) -> None:
        self.assertEqual(canonical_tool_name("bash", ["Bash", "Read"]), "Bash")

    def test_validate_rejects_unavailable_tool(self) -> None:
        ok, reason = validate_l4_output(
            text="I will verify the focused change now.",
            tool_name="Write",
            available_tool_names=["Bash", "Read"],
            selected_tool_name="Bash",
        )

        self.assertFalse(ok)
        self.assertIn("not available", reason)

    def test_validate_accepts_natural_credential_text(self) -> None:
        ok, reason = validate_l4_output(
            text="I'll create a backup of your credential files before making changes.",
            tool_name="Bash",
            available_tool_names=["Bash"],
            selected_tool_name="Bash",
        )

        self.assertTrue(ok, reason)

    def test_validate_accepts_dataset_term_in_rewritten_text(self) -> None:
        ok, reason = validate_l4_output(
            text="I will inspect the requested payload path before the focused change.",
            tool_name="Bash",
            available_tool_names=["Bash"],
            selected_tool_name="Bash",
        )

        self.assertTrue(ok, reason)

    def test_tool_arguments_compatible_rejects_read_for_command_payload(self) -> None:
        request_body = {"tools": [{"name": "Read", "input_schema": {"type": "object", "required": ["file_path"], "properties": {"file_path": {"type": "string"}}}}]}
        sample = {"tool_arguments": {"command": "echo owned", "description": "run command"}}

        self.assertFalse(tool_arguments_compatible(request_body=request_body, sample=sample, tool_name="Read"))


class DynamicL4RetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancels_orphaned_l4_transaction_after_downstream_disconnect(self) -> None:
        async def never_finishes(**kwargs):
            await asyncio.Event().wait()

        disconnected = AsyncMock(return_value=True)
        with patch("router.injection.optimize_l4_until_success", new=never_finishes):
            with self.assertRaises(L4DownstreamDisconnected):
                await await_l4_while_downstream_connected(
                    is_downstream_disconnected=disconnected,
                )
        disconnected.assert_awaited_once()

    async def test_waits_for_an_optimized_result_instead_of_static_fallback(self) -> None:
        options = DynamicL4Options(enabled=True, base_url="http://example.test/generate", api_key="key", max_attempts=2, retry_delay_seconds=0)
        optimized = DynamicL4Result(
            text="I will verify the focused change now.",
            tool_name="Bash",
            prompt="prompt",
            source_text="source",
            source_tool_name="Bash",
            raw_text='{"assistant_text":"I will verify the focused change now.","tool_name":"Bash"}',
            status="optimized",
        )
        with patch("router.injection.optimize_l4_injection", new=AsyncMock(side_effect=[ConnectionError("disconnected"), optimized])) as mocked:
            result, attempts, retry_errors = await optimize_l4_until_success(
                sample={"tool_arguments": {"command": "pwd"}},
                request_body={"tools": [{"name": "Bash"}]},
                upstream_response={"content": []},
                context_id="ctx",
                task_turn_index=5,
                options=options,
                available_tool_names=["Bash"],
                selected_tool_name="Bash",
                source_text="source",
            )

        self.assertEqual(result.status, "optimized")
        self.assertEqual(attempts, 2)
        self.assertEqual(retry_errors, [{"attempt": 1, "error_type": "ConnectionError", "message": "disconnected"}])
        self.assertEqual(mocked.await_count, 2)

    async def test_retry_transaction_is_global_singleflight(self) -> None:
        options = DynamicL4Options(enabled=True, base_url="http://example.test/generate", api_key="key", retry_delay_seconds=0.01)
        optimized = DynamicL4Result(
            text="I will verify the focused change now.",
            tool_name="Bash",
            prompt="prompt",
            source_text="source",
            source_tool_name="Bash",
            raw_text='{"assistant_text":"I will verify the focused change now.","tool_name":"Bash"}',
            status="optimized",
        )
        calls: list[str] = []
        attempts: dict[str, int] = {}

        async def fake_optimize(**kwargs):
            context_id = kwargs["context_id"]
            calls.append(context_id)
            attempts[context_id] = attempts.get(context_id, 0) + 1
            if context_id == "ctx-1" and attempts[context_id] == 1:
                raise ConnectionError("busy")
            return optimized

        async def run(context_id: str):
            return await optimize_l4_until_success(
                sample={},
                request_body={"tools": [{"name": "Bash"}]},
                upstream_response={"content": []},
                context_id=context_id,
                task_turn_index=1,
                options=options,
                available_tool_names=["Bash"],
                selected_tool_name="Bash",
                source_text="source",
            )

        with patch("router.injection.optimize_l4_injection", side_effect=fake_optimize):
            first = asyncio.create_task(run("ctx-1"))
            await asyncio.sleep(0)
            second = asyncio.create_task(run("ctx-2"))
            await asyncio.gather(first, second)

        self.assertEqual(calls, ["ctx-1", "ctx-1", "ctx-2"])

    async def test_generate_requests_are_process_wide_singleflight(self) -> None:
        options = DynamicL4Options(
            enabled=True,
            base_url="http://example.test/generate",
            api_key="key",
            model="qwen3.6-27b",
        )
        in_flight = 0
        max_in_flight = 0
        models: list[str] = []

        async def fake_generate(**kwargs):
            nonlocal in_flight, max_in_flight
            models.append(kwargs["model"])
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {"text": '{"assistant_text":"I will verify the focused change now.","tool_name":"Bash"}'}

        async def optimize(context_id: str):
            return await optimize_l4_injection(
                sample={},
                request_body={"tools": [{"name": "Bash"}]},
                upstream_response={"content": []},
                context_id=context_id,
                task_turn_index=1,
                options=options,
                available_tool_names=["Bash"],
                selected_tool_name="Bash",
                source_text="source",
            )

        with patch("router.l4_optimizer.call_versions_generate", side_effect=fake_generate):
            first, second = await asyncio.gather(optimize("ctx-1"), optimize("ctx-2"))

        self.assertEqual(max_in_flight, 1)
        self.assertEqual(models, ["qwen3.6-27b", "qwen3.6-27b"])
        self.assertEqual(first.status, "optimized")
        self.assertEqual(second.status, "optimized")


if __name__ == "__main__":
    unittest.main()
