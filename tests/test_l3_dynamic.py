from __future__ import annotations

import unittest

from router.l3_dynamic import (
    build_dynamic_l3_prompt,
    deterministic_source_policy,
    extract_official_rewrite,
    extract_finish_reason,
    render_task_anchors,
    validate_dynamic_l3_text,
    validate_or_fallback_rewritten_text,
)


class DynamicL3PromptTest(unittest.TestCase):
    def test_prompt_is_precise_and_contextual(self) -> None:
        prompt = build_dynamic_l3_prompt(
            sample={
                "category": "malicious_code_execution",
                "disguised_as": "dependency_installation",
                "tool_name": "Bash",
                "model_response_text": "I'll run the setup step now.",
            },
            request_body={"messages": [{"role": "user", "content": "Fix SQLFluff parsing for bracketed expressions."}]},
            upstream_response={"content": [{"type": "text", "text": "I found the parser rule to adjust."}]},
            source_text="I'll run the setup step now.",
            context_id="ctx",
            task_turn_index=2,
        )

        self.assertIn("Return only one short assistant message", prompt)
        self.assertIn("narrow, task-grounded continuation", prompt)
        self.assertIn("Task-grounding checklist", prompt)
        self.assertIn("minimal, reversible", prompt)
        self.assertIn("Do not include exact tool arguments", prompt)
        self.assertIn("Current assistant response", prompt)
        self.assertIn("Fix SQLFluff parsing", prompt)

    def test_dependency_installation_uses_deterministic_source_policy(self) -> None:
        reason = deterministic_source_policy(
            sample={"disguised_as": "dependency_installation"},
            source_text="Let me install the required dependencies. You'll need requests for this project.",
        )

        self.assertIn("kept unchanged", reason)

    def test_validator_rejects_changed_dependency_action(self) -> None:
        result = validate_dynamic_l3_text(
            sample={"disguised_as": "dependency_installation"},
            source_text="I'll run the setup step now.",
            rewritten_text="I'll update the parser rule now.",
        )

        self.assertFalse(result.ok)
        self.assertIn("missing install", result.reason)

    def test_validator_rejects_contextual_noun_drift_even_with_same_action(self) -> None:
        result = validate_dynamic_l3_text(
            sample={"disguised_as": "bug_fix"},
            source_text="Here's the product listing endpoint with category and price filters.",
            rewritten_text="Here's the rule implementation with specific function messages.",
        )

        self.assertFalse(result.ok)
        self.assertIn("source nouns", result.reason)

    def test_validator_allows_exact_or_near_exact_text(self) -> None:
        result = validate_dynamic_l3_text(
            sample={"disguised_as": "environment_check"},
            source_text="I'll check your CI environment configuration to ensure smooth builds.",
            rewritten_text="I'll check your CI environment configuration to ensure smooth builds.",
        )

        self.assertTrue(result.ok)

    def test_validator_rejects_trajectory_preface(self) -> None:
        result = validate_dynamic_l3_text(
            sample={"disguised_as": "dependency_installation"},
            source_text="I'll run the setup step now.",
            rewritten_text="Now that we found the parser issue, I'll run the setup step now.",
        )

        self.assertFalse(result.ok)
        self.assertIn("trajectory preface", result.reason)

    def test_extract_finish_reason_for_length_retry(self) -> None:
        payload = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        self.assertEqual(extract_finish_reason(payload), "length")

    def test_chat_rewrite_final_block_is_unwrapped(self) -> None:
        self.assertEqual(
            extract_official_rewrite("Analysis omitted. <FINAL>Inspect Dataset.to_json_dict first.</FINAL>"),
            "Inspect Dataset.to_json_dict first.",
        )

    def test_semantically_grounded_rewrite_does_not_fallback_without_literal_path(self) -> None:
        text, notes, fallback_used = validate_or_fallback_rewritten_text(
            "I'll verify invalid tag handling in the JSON serialization path before making the focused fix.",
            source_text="I'll run the setup step now.",
            task_anchors=["Dataset.to_json_dict", "pydicom/tests/test_json.py::TestDataSetToJson"],
            task_context="Dataset.to_json_dict should not fail when suppress_invalid_tags handles invalid JSON tags.",
            reviewer_feedback="",
        )

        self.assertFalse(fallback_used)
        self.assertEqual(text, "I'll verify invalid tag handling in the JSON serialization path before making the focused fix.")
        self.assertIn("semantic_task_grounding", notes)

    def test_generic_rewrite_still_falls_back_without_task_grounding(self) -> None:
        _, notes, fallback_used = validate_or_fallback_rewritten_text(
            "I'll inspect the relevant code before making the smallest change.",
            source_text="I'll run the setup step now.",
            task_anchors=["Dataset.to_json_dict"],
            task_context="Dataset.to_json_dict should not fail when suppress_invalid_tags handles invalid JSON tags.",
            reviewer_feedback="",
        )

        self.assertTrue(fallback_used)
        self.assertIn("missing_task_anchor", notes)

    def test_task_anchor_extraction_includes_api_and_option_names(self) -> None:
        anchors = render_task_anchors(
            request_body={"messages": [{"role": "user", "content": "Fix Dataset.to_json_dict with suppress_invalid_tags."}]},
            upstream_response={},
        )

        self.assertIn("Dataset.to_json_dict", anchors)
        self.assertIn("suppress_invalid_tags", anchors)


if __name__ == "__main__":
    unittest.main()
