from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from manager.cli import main
from manager.planner import parse_turns
from manager.router_process import RouterProcessSpec, SharedRouterLauncher, stop_router
from manager.workflow import add_run_arguments, router_environment, start_router
from router.config import Settings


class WorkflowTest(unittest.TestCase):
    def invoke(self, root, *options):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', [
            'manager.cli', 'run', '--run-id', 'test', '--artifacts-root', str(root),
            '--agents', 'claude', '--task-limit', '1', *options,
        ]), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        return output.getvalue()

    def manifests(self, root):
        return [json.loads(path.read_text()) for path in (root / 'test/experiments').glob('*/manifest.json')]

    def test_l3_dry_run_plans_contexts_without_execution_or_secrets(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('manager.workflow.start_router') as start, mock.patch('manager.workflow.run_containers') as containers, mock.patch('manager.workflow.analyze_run') as analyze:
            root = Path(tmp)
            output = self.invoke(root, '--dry-run', '--dynamic-l3', '--samples', '1:2', '--dynamic-l3-api-key', 'test-secret')
            manifests = self.manifests(root)
            self.assertEqual(len(manifests), 2)
            for manifest in manifests:
                self.assertTrue(manifest['dynamic_l3_enabled'])
                self.assertFalse(manifest['dynamic_l4_enabled'])
                self.assertFalse(manifest['payload_execution_allowed'])
                self.assertEqual(manifest['injection_turn'], 0)
                self.assertEqual(manifest['replay_match_mode'], 'exact_request_hash')
                self.assertNotIn('test-secret', json.dumps(manifest))
            registry = json.loads((root / 'test/router/effective_config.json').read_text())
            self.assertTrue(all(c['dynamic_l3_enabled'] for c in registry['contexts'].values()))
            self.assertNotIn('test-secret', json.dumps(registry) + output)
            self.assertFalse((root / 'test/analysis').exists())
            start.assert_not_called()
            containers.assert_not_called()
            analyze.assert_not_called()

    def test_live_workflow_orders_execution_cleanup_and_analysis(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch('manager.workflow.start_router', side_effect=lambda *a: events.append('start') or 'router'), mock.patch('manager.workflow.run_containers', side_effect=lambda *a: events.append('containers')), mock.patch('manager.workflow.stop_router', side_effect=lambda *a: events.append('stop')), mock.patch('manager.workflow.analyze_run', side_effect=lambda *a: events.append('analyze') or {}):
            root = Path(tmp)
            self.invoke(root, '--router-mode', 'inject', '--dynamic-l3', '--payload-execution-allowed')
            self.assertTrue(self.manifests(root)[0]['payload_execution_allowed'])
        self.assertEqual(events, ['start', 'containers', 'stop', 'analyze'])

    def test_container_failure_stops_router(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('manager.workflow.start_router', return_value='router'), mock.patch('manager.workflow.run_containers', side_effect=RuntimeError('container failed')), mock.patch('manager.workflow.stop_router') as stop:
            with self.assertRaisesRegex(RuntimeError, 'container failed'):
                self.invoke(Path(tmp), '--dynamic-l3')
            stop.assert_called_once_with('router')

    def test_record_and_replay_modes(self):
        for mode in ('proxy-record', 'replay'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                trace = root / 'traces'
                trace.mkdir()
                options = ['--trace-source-dir', str(trace)] if mode == 'replay' else []
                self.invoke(root, '--dry-run', '--router-mode', mode, '--samples', 'all', *options)
                manifests = self.manifests(root)
                self.assertEqual(len(manifests), 1)
                self.assertEqual(manifests[0]['dataset_sample_id'], '')
                self.assertIsNone(manifests[0]['injection_turn'])
                if mode == 'replay':
                    self.assertEqual(manifests[0]['trace_source_dir'], str(trace.resolve()))

    def test_random_turns_and_direct_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / 'traces'
            trace.mkdir()
            self.invoke(root, '--dry-run', '--dynamic-l3', '--injection-turn-mode', 'random', '--injection-turns', '2:4', '--injection-random-seed', '17', '--trace-source-dir', str(trace))
            manifest = self.manifests(root)[0]
            self.assertEqual(manifest['injection_turns'], [2, 3, 4])
            self.assertEqual(manifest['injection_random_seed'], 17)
            self.assertEqual(manifest['trace_source_dir'], str(trace.resolve()))

    def test_invalid_combinations_fail_before_writing(self):
        cases = [
            ['--dynamic-l3', '--dynamic-l4'],
            ['--dynamic-l3', '--router-mode', 'proxy-record'],
            ['--router-mode', 'replay'],
            ['--injection-turns', '3'],
            ['--injection-turn-mode', 'random', '--injection-turns', '0'],
            ['--dynamic-l3-backend', 'call_versions_official', '--dynamic-l3-call-version', '2'],
            ['--router-mode', 'inject', '--trace-source-dir', '.'],
            ['--trace-source-dir', '.', '--agents', 'claude,codex'],
        ]
        for options in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit):
                    self.invoke(Path(tmp), '--dry-run', *options)
                self.assertFalse((Path(tmp) / 'test').exists())
        with self.assertRaises(ValueError):
            parse_turns('-1', 'inject')

    def args(self, *options):
        parser = argparse.ArgumentParser()
        add_run_arguments(parser)
        return parser.parse_args(list(options))

    def test_service_options_reach_router_and_preserve_env_file(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            env_file = Path(tmp) / '.env'
            env_file.write_text('DYNAMIC_L3_API_KEY=file-key\nDYNAMIC_L3_MODEL=file-model\nDYNAMIC_L3_BACKEND=call_versions_official\n')
            defaults = router_environment(self.args('--dynamic-l3'))
            self.assertNotIn('DYNAMIC_L3_API_KEY', defaults)
            with mock.patch.dict(os.environ, defaults):
                settings = Settings(_env_file=env_file)
                self.assertEqual(settings.dynamic_l3_api_key, 'file-key')
                self.assertEqual(settings.dynamic_l3_model, 'file-model')
                self.assertEqual(settings.dynamic_l3_backend, 'call_versions_official')
            overrides = router_environment(self.args('--dynamic-l3', '--dynamic-l3-model', 'cli-model', '--dynamic-l3-max-attempts', '5', '--dynamic-l3-require-rewrite'))
            with mock.patch.dict(os.environ, overrides):
                settings = Settings(_env_file=env_file)
                self.assertEqual(settings.dynamic_l3_model, 'cli-model')
                self.assertEqual(settings.dynamic_l3_max_attempts, 5)
                self.assertTrue(settings.dynamic_l3_require_rewrite)
                self.assertEqual(settings.dynamic_l3_api_key, 'file-key')

    def test_l4_options_remain_available(self):
        args = self.args('--dynamic-l4', '--dynamic-l4-call-version', '3', '--dynamic-l4-alpha', '0.75')
        env = router_environment(args)
        self.assertEqual(env['DYNAMIC_L4_CALL_VERSION'], '3')
        self.assertEqual(env['DYNAMIC_L4_ALPHA'], '0.75')

    def test_startup_failure_cleans_up_child(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('manager.workflow.SharedRouterLauncher.start', return_value='router'), mock.patch('manager.workflow.wait_for_router', side_effect=TimeoutError), mock.patch('manager.workflow.stop_router') as stop:
            with self.assertRaises(TimeoutError):
                start_router(self.args(), Path(tmp), Path(tmp) / 'registry.json')
            stop.assert_called_once_with('router')

    def test_launcher_uses_active_interpreter_and_closes_log(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('manager.router_process.subprocess.Popen') as popen:
            spec = RouterProcessSpec('127.0.0.1', 18080, Path(tmp) / 'config.json', Path(tmp) / 'router.log')
            SharedRouterLauncher().start(spec)
            self.assertEqual(popen.call_args.args[0][0], sys.executable)
            self.assertTrue(popen.call_args.kwargs['stdout'].closed)
            self.assertEqual(popen.call_args.kwargs['env']['ROUTER_CONTEXT_REGISTRY'], str(spec.registry_path))

    def test_stop_escalates_if_child_ignores_terminate(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('uvicorn', 10), 0]
        stop_router(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
