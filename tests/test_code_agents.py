from __future__ import annotations

import json
import unittest

from code_agents import SUPPORTED_AGENT_NAMES, agent_definition, normalize_agent_name, require_agent_name
from manager.planner import parse_agents
from router.attack_dataset import raw_agent_arguments


class CodeAgentRegistryTest(unittest.TestCase):
    def test_registry_is_single_source_for_agent_validation(self) -> None:
        self.assertEqual(SUPPORTED_AGENT_NAMES, ("codex", "claude"))
        self.assertEqual(normalize_agent_name("unknown"), "codex")
        with self.assertRaisesRegex(ValueError, "unsupported agent: unknown"):
            require_agent_name("unknown")
        self.assertEqual(parse_agents("codex,claude"), ("codex", "claude"))

    def test_attack_argument_format_comes_from_agent_definition(self) -> None:
        self.assertEqual(agent_definition("codex").attack_argument_format, "codex_exec_json")
        codex_args = raw_agent_arguments(args={"command": "echo ok"}, agent="codex", tool_name="shell")
        self.assertEqual(json.loads(codex_args)["cmd"], "echo ok")
        claude_args = raw_agent_arguments(args={"command": "echo ok"}, agent="claude", tool_name="shell")
        self.assertEqual(claude_args, {"command": "echo ok"})


if __name__ == "__main__":
    unittest.main()
