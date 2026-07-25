from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from manager.executor import context_result_from_container
from swebench_container.models import ContainerRunResult, ContainerRunSpec
from swebench_container.runner import DockerTaskRunner, container_name_for, redact_command, serializable_spec, terminal_artifact_exists, wrapped_agent_script


class ContainerRunnerTest(unittest.TestCase):
    def test_codex_command_and_redacted_spec(self) -> None:
        spec = ContainerRunSpec(
            run_id="run",
            context_id="ctx",
            instance_id="task",
            agent="codex",
            agent_mode="auto_review",
            router_url="http://router:8000",
            router_api_key="rk_secret",
            artifact_dir=Path("/tmp/artifacts"),
            swebench_image="swebench/base",
            wrapper_image="wrapper",
            issue_prompt="fix bug",
        )
        command = DockerTaskRunner().build_docker_command(spec)
        self.assertIn("OPENAI_BASE_URL=http://router:8000/v1", command)
        self.assertIn("OPENAI_API_KEY=rk_secret", command)
        self.assertNotIn("--rm", command)
        self.assertIn("--name", command)
        self.assertEqual(command[-3:-1], ["sh", "-lc"])
        self.assertIn("mkdir -p", command[-1])
        self.assertIn("--ask-for-approval on-request", command[-1])
        self.assertIn('--config \'approvals_reviewer="auto_review"\'', command[-1])
        self.assertIn('--config \'model_provider="router"\'', command[-1])
        self.assertIn('wire_api="responses"', command[-1])
        self.assertIn("--sandbox workspace-write", command[-1])
        self.assertNotIn("--approval-policy", command[-1])
        self.assertIn("git diff --binary", command[-1])
        self.assertIn("$(cat /artifacts/prompt.md)", command[-1])
        self.assertNotIn("fix bug", command[-1])
        self.assertEqual(serializable_spec(spec)["router_api_key"], "<redacted>")
        redacted = redact_command(command)
        self.assertIn("OPENAI_API_KEY=<redacted>", redacted)
        self.assertNotIn("OPENAI_API_KEY=rk_secret", redacted)

    def test_claude_auto_review_uses_auto_permission_mode(self) -> None:
        spec = ContainerRunSpec(
            run_id="run",
            context_id="ctx",
            instance_id="task",
            agent="claude",
            agent_mode="auto_review",
            router_url="http://router:8000",
            router_api_key="rk_secret",
            artifact_dir=Path("/tmp/artifacts"),
            swebench_image="swebench/base",
            wrapper_image="wrapper",
            issue_prompt="fix bug",
        )

        command = DockerTaskRunner().build_docker_command(spec)

        self.assertIn("ANTHROPIC_BASE_URL=http://router:8000", command)
        self.assertIn("ANTHROPIC_API_KEY=rk_secret", command)
        self.assertIn("claude --permission-mode auto --print", command[-1])
        self.assertNotIn("--permission-mode default", command[-1])


    def test_wrapped_agent_script_exports_artifacts(self) -> None:
        script = wrapped_agent_script(["agent", "run", "hello world"])
        self.assertIn("agent run", script)
        self.assertIn("/artifacts/final.patch", script)
        self.assertIn("/artifacts/git.status.txt", script)

    def test_terminal_artifact_is_scoped_to_current_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = ContainerRunSpec(
                run_id="run",
                context_id="ctx-a",
                instance_id="task",
                agent="claude",
                agent_mode="auto_review",
                router_url="http://router:8000",
                router_api_key="rk_secret",
                artifact_dir=root / "experiments" / "ctx-a" / "container",
                swebench_image="image",
                wrapper_image="wrapper",
                issue_prompt="fix bug",
            )
            other_trace = root / "experiments" / "ctx-b" / "router" / "traces" / "0001-anthropic.messages"
            other_trace.mkdir(parents=True)
            (other_trace / "terminal.json").write_text("{}\n", encoding="utf-8")

            self.assertFalse(terminal_artifact_exists(spec))

            own_trace = root / "experiments" / "ctx-a" / "router" / "traces" / "0001-anthropic.messages"
            own_trace.mkdir(parents=True)
            (own_trace / "terminal.json").write_text("{}\n", encoding="utf-8")

            self.assertTrue(terminal_artifact_exists(spec))

    def test_payload_blocked_container_result_is_preserved(self) -> None:
        result = context_result_from_container(
            ContainerRunResult(
                context_id="ctx",
                status="payload_blocked",
                exit_code=137,
                timed_out=False,
                stdout_path=Path("stdout.log"),
                stderr_path=Path("stderr.log"),
                artifact_dir=Path("artifacts"),
            )
        )

        self.assertEqual(result.status, "payload_blocked")

    def test_container_name_is_stable_and_unique_for_long_shared_prefixes(self) -> None:
        shared = "claude-allowall-inject-random10-replace-defense-sqlfluff1625-all400-par4-timeout600-20260627-004457-sqlfluff__sqlfluff-1625-claude-allow_all-"
        spec_a = ContainerRunSpec(
            run_id="run",
            context_id=shared + "buggy-003-turn-random",
            instance_id="task",
            agent="claude",
            agent_mode="allow_all",
            router_url="http://router:8000",
            router_api_key="rk_secret",
            artifact_dir=Path("/tmp/artifacts-a"),
            swebench_image="image",
            wrapper_image="wrapper",
            issue_prompt="fix bug",
        )
        spec_b = ContainerRunSpec(
            run_id="run",
            context_id=shared + "buggy-004-turn-random",
            instance_id="task",
            agent="claude",
            agent_mode="allow_all",
            router_url="http://router:8000",
            router_api_key="rk_secret",
            artifact_dir=Path("/tmp/artifacts-b"),
            swebench_image="image",
            wrapper_image="wrapper",
            issue_prompt="fix bug",
        )

        name_a = container_name_for(spec_a)
        name_b = container_name_for(spec_b)

        self.assertNotEqual(name_a, name_b)
        self.assertEqual(name_a, container_name_for(spec_a))
        self.assertTrue(name_a.startswith("swebench-run-"))
        self.assertTrue(name_b.startswith("swebench-run-"))


if __name__ == "__main__":
    unittest.main()
