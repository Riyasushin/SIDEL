from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from code_agents import resolve_agent_mode
from manager.planner import MatrixPlanOptions, build_matrix_plan, parse_agents
from swebench_container.agents import adapter_for
from swebench_container.models import ContainerRunSpec
from swebench_container.runner import DockerTaskRunner, redact_command, serializable_spec
from router.attack_dataset import raw_agent_arguments
from router.context import normalize_agent_mode
from router.config import Settings
from router.context import single_context_from_settings
from router.tracing import extract_trace_metadata, reset_turn_counters


class NativeAgentsTest(unittest.TestCase):
    def spec(self, agent, mode):
        return ContainerRunSpec('run', 'ctx', 'task', agent, mode, 'http://router:8000', 'routing-secret',
                                Path('/tmp/artifacts'), 'base', 'wrapper', 'Fix the issue')

    def test_all_native_modes_and_legacy_aliases(self):
        modes = {'claude': ('plan', 'acceptEdits', 'auto', 'bypassPermissions'),
                 'codex': ('auto', 'full_access'), 'cursor': ('auto_review', 'run_everything'),
                 'opencode': ('plan', 'build')}
        for agent, values in modes.items():
            for mode in values:
                with self.subTest(agent=agent, mode=mode):
                    self.assertEqual(resolve_agent_mode(agent, mode), mode)
                    self.assertEqual(normalize_agent_mode(mode), mode)
                    self.assertTrue(adapter_for(agent).build_command(self.spec(agent, mode)))
            with self.assertRaises(ValueError):
                adapter_for(agent).build_command(self.spec(agent, 'typo'))
        self.assertEqual(resolve_agent_mode('claude', 'allow_all'), 'acceptEdits')
        self.assertEqual(resolve_agent_mode('codex', 'allow_all'), 'full_access')
        self.assertEqual(parse_agents('claudecode,opencode'), ('claude', 'opencode'))

    def test_claude_permissions_and_non_root_bypass(self):
        for mode in ('plan', 'acceptEdits', 'auto', 'bypassPermissions'):
            spec = self.spec('claude', mode)
            command = adapter_for('claude').build_command(spec)
            self.assertEqual(command[command.index('--permission-mode') + 1], mode)
            script = DockerTaskRunner().build_docker_command(spec)[-1]
            self.assertEqual('runuser -u sidel' in script, mode == 'bypassPermissions')

    def test_codex_full_access_keeps_router_provider(self):
        for mode in ('auto', 'full_access'):
            command = adapter_for('codex').build_command(self.spec('codex', mode))
            self.assertIn('model_provider="router"', command)
            self.assertEqual('--dangerously-bypass-approvals-and-sandbox' in command, mode == 'full_access')
            self.assertEqual('approvals_reviewer="auto_review"' in command, mode == 'auto')

    def test_opencode_plan_keeps_native_permissions_build_allows(self):
        adapter = adapter_for('opencode')
        for mode in ('plan', 'build'):
            spec = self.spec('opencode', mode)
            env = adapter.build_env(spec)
            config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
            self.assertEqual(config.get('permission'), 'allow' if mode == 'build' else None)
            self.assertNotIn('agent', config)
            self.assertEqual(config['provider']['sidel']['options']['baseURL'], 'http://router:8000/v1')
            self.assertNotIn('routing-secret', env['OPENCODE_CONFIG_CONTENT'])
            self.assertIn(mode, adapter.build_command(spec))

    def test_cursor_modes_keep_native_transport_and_redact_credentials(self):
        for mode, flag in [('auto_review', '--auto-review'), ('run_everything', '--yolo')]:
            spec = self.spec('cursor', mode)
            adapter = adapter_for('cursor')
            command = adapter.build_command(spec)
            self.assertIn(flag, command)
            self.assertFalse('--auto-review' in command and '--yolo' in command)
            env = adapter.build_env(spec)
            self.assertEqual(env['CURSOR_API_ENDPOINT'], 'http://router:8000/cursor')
            config = json.loads(env['SIDEL_CURSOR_CONFIG'])
            self.assertTrue(config['network']['useHttp1ForAgent'])
            docker = DockerTaskRunner().build_docker_command(spec)
            self.assertNotIn('routing-secret', json.dumps(redact_command(docker)))
            spec = replace(spec, environment={'CURSOR_API_KEY': 'account-secret'})
            self.assertNotIn('account-secret', json.dumps(serializable_spec(spec)))

    def test_opencode_argument_translation_preserves_values(self):
        args = {'file_path': '/testbed/example.py', 'old_string': 'x=1', 'new_string': 'x=2'}
        converted = raw_agent_arguments(args=args, agent='opencode', tool_name='edit')
        self.assertEqual(converted, {'filePath': '/testbed/example.py', 'oldString': 'x=1', 'newString': 'x=2'})
        self.assertIn('file_path', args)

    def test_opencode_titles_do_not_advance_task_turns(self):
        reset_turn_counters()
        context = single_context_from_settings(Settings(_env_file=None, ROUTER_TRACE_AGENT='opencode'))
        title = {'messages': [{'role': 'system', 'content': 'You are a title generator. You output ONLY a thread title.'},
                              {'role': 'user', 'content': 'Fix the issue'}]}
        task = {'messages': [{'role': 'user', 'content': 'Fix the issue'}], 'tools': []}
        self.assertIsNone(extract_trace_metadata(title, context, 'openai.chat')['task_turn_index'])
        self.assertEqual(extract_trace_metadata(task, context, 'openai.chat')['task_turn_index'], 0)
        self.assertIsNone(extract_trace_metadata(title, context, 'openai.chat')['task_turn_index'])
        self.assertEqual(extract_trace_metadata(task, context, 'openai.chat')['task_turn_index'], 1)

    def test_planner_selects_per_agent_defaults_and_supports_cursor_modes(self):
        options = MatrixPlanOptions('run', 'proxy-record', ('codex', 'claude', 'cursor', 'opencode'),
                                    None, ('task',), ('sample',), (None,), 'replace', False, 300)
        with mock.patch('manager.planner.default_swebench_image', return_value='base'):
            manifests = list(build_matrix_plan(options).values())
            self.assertEqual({m.agent: m.agent_mode for m in manifests},
                             {'codex': 'auto_review', 'claude': 'auto_review', 'cursor': 'auto_review', 'opencode': 'plan'})
            for mode in ('inject', 'replay', 'replay-inject'):
                plan = build_matrix_plan(replace(options, agents=('cursor',), router_mode=mode,
                                                dynamic_l3_enabled=mode != 'replay', enable_tool_defense=True))
                self.assertEqual(next(iter(plan.values())).router_mode, mode)
