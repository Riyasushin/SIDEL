from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from router.config import Settings
from router.providers.registry import build_providers
from scripts.replay_reviewer_runs import (
    build_chat_request,
    build_settings_for_provider,
    default_output_names,
    endpoint_for_reviewer_dir,
    iter_failed_reviewer_dirs,
    validate_concurrency,
    write_json,
)


class ReviewerRerunScriptTest(unittest.TestCase):
    def test_endpoint_for_reviewer_dir_prefers_metadata_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reviewer_dir = Path(tmp) / "0000-reviewer-anthropic.messages"
            reviewer_dir.mkdir(parents=True)
            (reviewer_dir / "metadata.json").write_text(json.dumps({"endpoint": "openai.responses"}), encoding="utf-8")

            self.assertEqual(endpoint_for_reviewer_dir(reviewer_dir), "openai.responses")

    def test_build_chat_request_converts_anthropic_request(self) -> None:
        body = {
            "model": "claude-review",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "<transcript>hello</transcript>"}]}],
        }

        chat_body = build_chat_request(body, endpoint="anthropic.messages", model="deepseek-v4-flash")

        self.assertEqual(chat_body["model"], "deepseek-v4-flash")
        self.assertEqual(chat_body["messages"][0]["role"], "user")
        self.assertFalse(chat_body["stream"])

    def test_build_chat_request_converts_openai_responses_request(self) -> None:
        body = {
            "model": "review-model",
            "max_output_tokens": 64,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        }

        chat_body = build_chat_request(body, endpoint="openai.responses", model="deepseek-v4-flash")

        self.assertEqual(chat_body["model"], "deepseek-v4-flash")
        self.assertFalse(chat_body["stream"])

    def test_validate_concurrency_accepts_positive_values(self) -> None:
        self.assertEqual(validate_concurrency(6), 6)

    def test_validate_concurrency_rejects_non_positive_values(self) -> None:
        with self.assertRaises(ValueError):
            validate_concurrency(0)

    def test_settings_alias_override_keeps_flash_model_for_provider(self) -> None:
        settings = Settings(DEFAULT_PROVIDER="deepseek", DEEPSEEK_MODEL="deepseek-v4-flash")
        provider = build_providers(settings)["deepseek"]

        self.assertEqual(provider.model, "deepseek-v4-flash")

    def test_build_settings_for_kimi_provider_keeps_requested_model(self) -> None:
        settings = build_settings_for_provider("kimi", "kimi-k2.7-code")
        provider = build_providers(settings)["kimi"]

        self.assertEqual(provider.model, "kimi-k2.7-code")

    def test_default_output_names_are_model_specific(self) -> None:
        rerun_dirname, summary_name, details_name, combined_summary_path = default_output_names("kimi-k2.7-code")

        self.assertEqual(rerun_dirname, "reviewer_reruns/kimi-k2.7-code")
        self.assertEqual(summary_name, "reviewer-rerun-kimi-k2.7-code-summary.json")
        self.assertEqual(details_name, "reviewer-rerun-kimi-k2.7-code-details.jsonl")
        self.assertEqual(combined_summary_path, "artifacts/reviewer-rerun-kimi-k2.7-code-combined-summary.json")

    def test_iter_failed_reviewer_dirs_returns_only_unsuccessful_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "run"
            reviewer_dir = run_root / "experiments" / "ctx" / "router" / "traces" / "0000-reviewer-anthropic.messages"
            reviewer_dir.mkdir(parents=True)
            (reviewer_dir / "request.agent.json").write_text("{}", encoding="utf-8")
            rerun_dir = run_root / "reviewer_reruns" / "kimi-k2.7-code" / "ctx" / "0000-reviewer-anthropic.messages"
            rerun_dir.mkdir(parents=True)
            write_json(
                rerun_dir / "source_ref.json",
                {"source_request": str(reviewer_dir / "request.agent.json")},
            )
            write_json(rerun_dir / "error.json", {"status": "error"})

            failed = iter_failed_reviewer_dirs(run_root, "reviewer_reruns/kimi-k2.7-code")

            self.assertEqual(failed, [reviewer_dir])

            write_json(rerun_dir / "meta.json", {"status": "ok"})

            failed_after_success = iter_failed_reviewer_dirs(run_root, "reviewer_reruns/kimi-k2.7-code")

            self.assertEqual(failed_after_success, [])


if __name__ == "__main__":
    unittest.main()
