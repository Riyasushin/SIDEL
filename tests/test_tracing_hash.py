from __future__ import annotations

import json
import unittest

from router.tracing import stable_hash


def request(device_id: str, session_id: str, cch: str, semantic_system_suffix: str = "") -> dict[str, object]:
    return {
        "model": "kimi-for-coding",
        "metadata": {
            "user_id": json.dumps(
                {
                    "device_id": device_id,
                    "account_uuid": "",
                    "session_id": session_id,
                    "organization_uuid": "org-stable",
                },
                separators=(",", ":"),
            )
        },
        "system": [
            {
                "type": "text",
                "text": (
                    f"x-anthropic-billing-header: cc_version=2.1.168.afc; cc_entrypoint=sdk-cli; cch={cch};\n"
                    f"Stable system rules.{semantic_system_suffix}"
                ),
            }
        ],
        "messages": [{"role": "user", "content": "solve task"}],
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    }


class StableHashTest(unittest.TestCase):
    def test_claude_session_metadata_and_billing_header_are_normalized(self) -> None:
        left = request("device-a", "session-a", "b49e0")
        right = request("device-b", "session-b", "81fb5")
        self.assertEqual(stable_hash(left), stable_hash(right))

    def test_system_prompt_semantics_still_affect_hash(self) -> None:
        left = request("device-a", "session-a", "b49e0")
        right = request("device-b", "session-b", "81fb5", " New permission rule.")
        self.assertNotEqual(stable_hash(left), stable_hash(right))


if __name__ == "__main__":
    unittest.main()
