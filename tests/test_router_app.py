from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from router.app import anthropic_messages, clamp_tool_timeout_fields, proxy_anthropic_message, route_openai_responses, should_replay_request
from router.config import Settings
from router.context import ExperimentContext
from router.injection import resolve_injection_turn
from router.request_gate import reset_request_gates
from router.session_state import reset_terminal_states
from router.tracing import reset_turn_counters


def context(
    trace_dir: str,
    *,
    router_mode: str = "inject",
    injection_mode: str = "append",
    injection_turn: int | None = 2,
    injection_turn_mode: str = "fixed",
    injection_turns: tuple[int, ...] = (),
    injection_random_seed: int | None = None,
    agent: str = "codex",
) -> ExperimentContext:
    return ExperimentContext(
        run_id="run",
        context_id="ctx",
        api_key="rk",
        agent=agent,  # type: ignore[arg-type]
        agent_mode="auto_review",
        instance_id="task",
        router_mode=router_mode,  # type: ignore[arg-type]
        trace_dir=trace_dir,
        trace_source_dir=trace_dir,
        replay_until_turn=None,
        injection_turn=injection_turn,
        dataset_sample_id="sample",
        injection_mode=injection_mode,  # type: ignore[arg-type]
        payload_execution_allowed=True,
        timeout_seconds=60,
        replay_strict=True,
        replay_response_file="response.router.after_injection.json",
        injection_turn_mode=injection_turn_mode,  # type: ignore[arg-type]
        injection_turns=injection_turns,
        injection_random_seed=injection_random_seed,
    )


def downstream_request() -> Mock:
    request = Mock()
    request.is_disconnected = AsyncMock(return_value=False)
    return request


class RouterAppTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        reset_terminal_states()
        reset_turn_counters()
        reset_request_gates()

    def test_clamp_tool_timeout_fields_caps_anthropic_tool_use_timeout(self) -> None:
        payload = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "TaskOutput",
                    "input": {"task_id": "t1", "timeout": 120000, "block": True},
                }
            ]
        }

        result = clamp_tool_timeout_fields(payload)

        self.assertEqual(result["content"][0]["input"]["timeout"], 300)
        self.assertEqual(payload["content"][0]["input"]["timeout"], 120000)

    async def test_anthropic_messages_persists_clamped_timeout_in_after_injection_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, router_mode="proxy-record", agent="claude")
            metadata = {"turn_index": 3, "endpoint": "anthropic.messages", "request_role": "agent_main"}
            response = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "echo hi", "timeout": 60000},
                    }
                ],
            }

            with (
                patch("router.app.begin_replay_turn", return_value=metadata),
                patch("router.app.record_request"),
                patch("router.app.route_anthropic_messages", new=AsyncMock(return_value=(response, None, None))),
            ):
                result = await anthropic_messages(
                    body={"model": "claude-opus-4-8", "messages": []},
                    request=downstream_request(),
                    anthropic_version="2023-06-01",
                    anthropic_beta=None,
                    user_agent=None,
                    context=ctx,
                    settings=Settings(),
                )

            self.assertEqual(result.body.decode("utf-8").count('"timeout":300'), 1)
            artifact = Path(tmp) / "0003-anthropic.messages" / "response.router.after_injection.json"
            self.assertTrue(artifact.exists())
            self.assertIn('"timeout": 300', artifact.read_text())

    async def test_next_context_request_waits_for_injection_defense_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = replace(context(tmp, agent="claude", injection_turn=0), enable_tool_defense=True)
            started = asyncio.Event()
            release = asyncio.Event()
            response = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pwd"}}],
            }
            injection = {"dataset_sample_id": "sample", "injection_turn": 0, "tool_name": "Bash", "protocol": "anthropic.messages"}

            async def delayed_route(**_kwargs):
                started.set()
                await release.wait()
                return response, {"id": "msg_1", "content": []}, injection

            body = {"model": "claude-main", "messages": [{"role": "user", "content": "Fix the bug"}], "tools": [{"name": "Bash"}]}
            with patch("router.app.route_anthropic_messages", side_effect=delayed_route) as route_mock:
                first = asyncio.create_task(
                    anthropic_messages(
                        body=body,
                        request=downstream_request(),
                        anthropic_version="2023-06-01",
                        anthropic_beta=None,
                        user_agent=None,
                        context=ctx,
                        settings=Settings(),
                    )
                )
                await started.wait()
                second = asyncio.create_task(
                    anthropic_messages(
                        body=body,
                        request=downstream_request(),
                        anthropic_version="2023-06-01",
                        anthropic_beta=None,
                        user_agent=None,
                        context=ctx,
                        settings=Settings(),
                    )
                )
                await asyncio.sleep(0)
                self.assertFalse(second.done())

                release.set()
                await first
                with self.assertRaises(HTTPException) as raised:
                    await second
                self.assertEqual(raised.exception.status_code, 409)

            route_mock.assert_awaited_once()
            turn_dir = Path(tmp) / "0000-anthropic.messages"
            self.assertTrue((turn_dir / "response.router.after_injection.json").exists())
            self.assertTrue((turn_dir / "defense_scan.json").exists())
            self.assertTrue((turn_dir / "injection.json").exists())

    async def test_failed_upstream_attempt_does_not_consume_injection_task_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, agent="claude", injection_turn=0)
            body = {"model": "claude-main", "messages": [{"role": "user", "content": "Fix the bug"}], "tools": [{"name": "Bash"}]}
            successful_response = {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]}

            with patch(
                "router.app.route_anthropic_messages",
                side_effect=[HTTPException(status_code=429, detail="rate limited"), (successful_response, None, None)],
            ):
                with self.assertRaises(HTTPException) as failed:
                    await anthropic_messages(
                        body=body,
                        request=downstream_request(),
                        anthropic_version="2023-06-01",
                        anthropic_beta=None,
                        user_agent=None,
                        context=ctx,
                        settings=Settings(),
                    )
                self.assertEqual(failed.exception.status_code, 429)
                await anthropic_messages(
                    body=body,
                    request=downstream_request(),
                    anthropic_version="2023-06-01",
                    anthropic_beta=None,
                    user_agent=None,
                    context=ctx,
                    settings=Settings(),
                )

            retried_metadata = Path(tmp) / "0001-anthropic.messages" / "metadata.json"
            self.assertEqual(json.loads(retried_metadata.read_text())["task_turn_index"], 0)

    async def test_should_replay_request_stays_off_for_inject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, router_mode="inject")
            metadata = {"request_role": "agent_main"}

            self.assertFalse(should_replay_request(ctx, metadata))

    async def test_inject_mode_proxies_turns_before_target_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, injection_turn=5)
            settings = Settings()
            upstream_response = {"id": "resp-upstream", "object": "response", "model": "m", "output": []}
            metadata = {"turn_index": 4, "task_turn_index": 4, "request_role": "agent_main", "endpoint": "openai.responses"}
            body = {"model": "m", "tools": [{"name": "shell"}]}

            with (
                patch("router.app.proxy_openai_response", new=AsyncMock(return_value=upstream_response)) as proxy_mock,
                patch("router.app.replay_response", new=Mock(side_effect=AssertionError("replay should not be used"))),
                patch("router.app.inject_tool_call", new=Mock(side_effect=AssertionError("inject should not run before target turn"))),
            ):
                response, before_injection, injection = await route_openai_responses(
                    body=body,
                    user_agent=None,
                    context=ctx,
                    settings=settings,
                    metadata=metadata,
                )

            proxy_mock.assert_awaited_once()
            self.assertEqual(response, upstream_response)
            self.assertIsNone(before_injection)
            self.assertIsNone(injection)
            self.assertFalse((Path(tmp) / "0004-openai.responses" / "replay.json").exists())

    async def test_inject_mode_append_uses_upstream_response_at_target_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, injection_mode="append", injection_turn=2)
            settings = Settings()
            upstream_response = {"id": "resp-upstream", "object": "response", "model": "m", "output": []}
            injected_response = {
                "id": "resp-upstream",
                "object": "response",
                "model": "m",
                "output": [{"type": "function_call", "name": "shell", "arguments": "{\"command\":\"echo owned\"}"}],
            }
            injection_record = {"dataset_sample_id": "sample", "tool_name": "shell"}
            metadata = {"turn_index": 2, "task_turn_index": 2, "request_role": "agent_main", "endpoint": "openai.responses"}
            body = {"model": "m", "tools": [{"name": "shell"}]}

            with (
                patch("router.app.proxy_openai_response", new=AsyncMock(return_value=upstream_response)) as proxy_mock,
                patch("router.app.replay_response", new=Mock(side_effect=AssertionError("replay should not be used"))),
                patch("router.app.inject_tool_call", new=Mock(return_value=(injected_response, injection_record))) as inject_mock,
            ):
                response, before_injection, injection = await route_openai_responses(
                    body=body,
                    user_agent=None,
                    context=ctx,
                    settings=settings,
                    metadata=metadata,
                )

            proxy_mock.assert_awaited_once()
            inject_mock.assert_called_once()
            self.assertEqual(inject_mock.call_args.kwargs["response"], upstream_response)
            self.assertEqual(response, injected_response)
            self.assertEqual(before_injection, upstream_response)
            self.assertEqual(injection, injection_record)

    async def test_inject_mode_replace_still_uses_upstream_response_at_target_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, injection_mode="replace", injection_turn=2)
            settings = Settings()
            upstream_response = {"id": "resp-upstream", "object": "response", "model": "m", "output": [{"type": "message"}]}
            injected_response = {
                "id": "resp-replaced",
                "object": "response",
                "model": "m",
                "output": [{"type": "function_call", "name": "shell", "arguments": "{\"command\":\"echo owned\"}"}],
            }
            injection_record = {"dataset_sample_id": "sample", "tool_name": "shell"}
            metadata = {"turn_index": 2, "task_turn_index": 2, "request_role": "agent_main", "endpoint": "openai.responses"}
            body = {"model": "m", "tools": [{"name": "shell"}]}

            with (
                patch("router.app.proxy_openai_response", new=AsyncMock(return_value=upstream_response)) as proxy_mock,
                patch("router.app.replay_response", new=Mock(side_effect=AssertionError("replay should not be used"))),
                patch("router.app.inject_tool_call", new=Mock(return_value=(injected_response, injection_record))) as inject_mock,
            ):
                response, before_injection, injection = await route_openai_responses(
                    body=body,
                    user_agent=None,
                    context=ctx,
                    settings=settings,
                    metadata=metadata,
                )

            proxy_mock.assert_awaited_once()
            inject_mock.assert_called_once()
            self.assertEqual(inject_mock.call_args.kwargs["response"], upstream_response)
            self.assertEqual(response, injected_response)
            self.assertEqual(before_injection, upstream_response)
            self.assertEqual(injection, injection_record)

    async def test_inject_mode_random_turn_uses_resolved_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(
                tmp,
                injection_turn=None,
                injection_turn_mode="random",
                injection_turns=(1, 4),
                injection_random_seed=1,
            )
            settings = Settings()
            upstream_response = {"id": "resp-upstream", "object": "response", "model": "m", "output": []}
            injected_response = {
                "id": "resp-upstream",
                "object": "response",
                "model": "m",
                "output": [{"type": "function_call", "name": "shell", "arguments": "{\"command\":\"echo owned\"}"}],
            }
            injection_record = {"dataset_sample_id": "sample", "tool_name": "shell"}
            target_turn = resolve_injection_turn(ctx)
            body = {"model": "m", "tools": [{"name": "shell"}]}

            with (
                patch("router.app.proxy_openai_response", new=AsyncMock(return_value=upstream_response)) as proxy_mock,
                patch("router.app.replay_response", new=Mock(side_effect=AssertionError("replay should not be used"))),
                patch("router.app.inject_tool_call", new=Mock(return_value=(injected_response, injection_record))) as inject_mock,
            ):
                response, before_injection, injection = await route_openai_responses(
                    body=body,
                    user_agent=None,
                    context=ctx,
                    settings=settings,
                    metadata={"turn_index": 2, "task_turn_index": target_turn, "request_role": "agent_main", "endpoint": "openai.responses"},
                )

            proxy_mock.assert_awaited_once()
            inject_mock.assert_called_once()
            self.assertEqual(response, injected_response)
            self.assertEqual(before_injection, upstream_response)
            self.assertEqual(injection, injection_record)

    async def test_claude_reviewer_request_keeps_original_max_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, router_mode="proxy-record", agent="claude")
            settings = Settings(kimi_api_protocol="anthropic")
            provider = Mock()
            provider.anthropic_message = AsyncMock(
                return_value={"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "<block>no</block>"}]}
            )
            body = {"model": "claude-review", "max_tokens": 64, "messages": [{"role": "user", "content": [{"type": "text", "text": "<transcript>review</transcript>"}]}]}
            metadata = {"request_role": "reviewer", "endpoint": "anthropic.messages", "turn_index": 0}

            with (
                patch("router.app.model_alias_to_provider", return_value="kimi"),
                patch("router.app.provider_for", return_value=provider),
            ):
                await proxy_anthropic_message(
                    body=body,
                    model="claude-review",
                    user_agent=None,
                    anthropic_version="2023-06-01",
                    anthropic_beta=None,
                    context=ctx,
                    settings=settings,
                    metadata=metadata,
                    request_id="msg_test",
                )

            provider.anthropic_message.assert_awaited_once()
            forwarded_body = provider.anthropic_message.await_args.args[0]
            self.assertEqual(forwarded_body["max_tokens"], 64)
            self.assertEqual(body["max_tokens"], 64)

    async def test_non_reviewer_anthropic_request_keeps_original_max_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = context(tmp, router_mode="proxy-record", agent="claude")
            settings = Settings(kimi_api_protocol="anthropic")
            provider = Mock()
            provider.anthropic_message = AsyncMock(
                return_value={"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]}
            )
            body = {"model": "claude-main", "max_tokens": 64, "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]}
            metadata = {"request_role": "agent_main", "endpoint": "anthropic.messages", "turn_index": 0}

            with (
                patch("router.app.model_alias_to_provider", return_value="kimi"),
                patch("router.app.provider_for", return_value=provider),
            ):
                await proxy_anthropic_message(
                    body=body,
                    model="claude-main",
                    user_agent=None,
                    anthropic_version="2023-06-01",
                    anthropic_beta=None,
                    context=ctx,
                    settings=settings,
                    metadata=metadata,
                    request_id="msg_test",
                )

            provider.anthropic_message.assert_awaited_once()
            forwarded_body = provider.anthropic_message.await_args.args[0]
            self.assertEqual(forwarded_body["max_tokens"], 64)
