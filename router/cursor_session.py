"""Intervene on native Cursor model steps while retaining its CLI executors."""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime, timezone

from router import cursor_wire as wire
from router.context import ExperimentContext
from router.injection import inject_tool_call_async, should_inject
from router.session_state import mark_injection_completed
from router.tool_defense import evaluate_tool_call
from router.tracing import next_turn_index, stable_hash, write_turn_artifact


class NativeStop(Exception):
    """Stop this native run after persisting the reason."""


class CursorSession:
    def __init__(self, context: ExperimentContext):
        self.context = context
        self.prompt = ''
        self.model = ''
        self.ready = asyncio.Event()
        self.changed = asyncio.Condition()
        self.sequences: dict[str, set[int]] = {}
        self.inputs: dict[tuple[str, int], bytes] = {}
        self.rpc_counts: dict[str, int] = {}
        self.task_turn = 0
        self.metadata: dict | None = None
        self.request: dict = {}
        self.content: list[dict] = []
        self.scan = {'scanned': 0, 'allowed': 0, 'denied': 0, 'entries': []}
        self.injected = False
        self.terminal = False
        self.active_stream = False
        self.pending: dict[int, tuple[int, asyncio.Future]] = {}
        self.local_pending: set[int] = set()
        self.replay_record: dict | None = None
        self.injection: dict | None = None
        self.history: list[dict] = []

    def ordinal(self, path: str) -> int:
        result = self.rpc_counts.get(path, 0)
        self.rpc_counts[path] = result + 1
        return result

    async def observe_client(self, request_id: str, seq: int, body: bytes):
        key = (request_id, seq)
        old = self.inputs.get(key)
        if old is not None and old != body:
            raise wire.WireError('conflicting native append retry')
        prompt, model = wire.user_prompt(body)
        if prompt:
            self.prompt, self.model = prompt, model
            self.ready.set()
        consumed = False
        result = wire.get(body, 2)
        if result:
            exec_id = int(wire.get(result, 1, 0))
            pending = self.pending.get(exec_id)
            if pending is not None:
                consumed = True
                kind, future = pending
                if not wire.get(result, kind):
                    raise wire.WireError('native tool result does not match requested tool')
                if not future.done():
                    future.set_result(result)
            self.history.append({'role': 'user', 'content': [{'type': 'native_tool_result',
                                 'native_id': exec_id, 'protobuf_base64': base64.b64encode(result).decode()}]})
        self.inputs[key] = body
        async with self.changed:
            self.sequences.setdefault(request_id, set()).add(seq)
            self.changed.notify_all()
        return consumed

    def last_sequence(self, request_id: str) -> int:
        # Use a contiguous watermark: concurrent HTTP appends can arrive out of order.
        seq = -1
        seen = self.sequences.get(request_id, set())
        while seq + 1 in seen:
            seq += 1
        return seq

    async def wait_sequence(self, request_id: str, seq: int):
        async with asyncio.timeout(self.context.timeout_seconds):
            async with self.changed:
                await self.changed.wait_for(lambda: self.last_sequence(request_id) >= seq)

    def save(self, name: str, payload):
        if self.metadata is not None:
            write_turn_artifact(self.context, self.metadata, name, payload)

    def start_step(self, step_id: int):
        if self.terminal:
            raise NativeStop('context completed')
        self.finish_step()
        self.metadata = {
            **self.context.public_metadata(), 'endpoint': 'cursor.rpc', 'request_role': 'agent_main',
            'turn_index': next_turn_index(self.context, 'agent_main'), 'task_turn_index': self.task_turn,
            'native_step_id': step_id, 'turn_basis': 'cursor.step_started', 'native_cli_version': wire.CLI_VERSION,
            'started_at': datetime.now(timezone.utc).isoformat(),
        }
        self.task_turn += 1
        self.request = {'model': self.model, 'messages': [{'role': 'user', 'content': self.prompt}] + self.history[-16:],
                        'tools': [{'name': name} for name in ('Bash', 'Write', 'Edit')],
                        'native_cli_version': wire.CLI_VERSION, 'native_step_id': step_id}
        self.content = []
        self.scan = {'scanned': 0, 'allowed': 0, 'denied': 0, 'entries': []}
        self.save('metadata.json', self.metadata)
        self.save('manifest.json', self.context.public_metadata())
        self.save('request.agent.json', self.request)
        if self.replay_record:
            self.save('replay.json', self.replay_record)

    def finish_step(self, *, failed: bool = False):
        if self.metadata is None:
            return
        response = {'type': 'cursor.native_step', 'content': self.content}
        self.metadata.update(finished_at=datetime.now(timezone.utc).isoformat(),
                             response_hash=stable_hash(response), status='failed' if failed else 'completed')
        self.save('metadata.json', self.metadata)
        if not self.injected:
            self.save('response.router.after_injection.json', response)
        self.history.append({'role': 'assistant', 'content': list(self.content)})
        if self.context.enable_tool_defense:
            self.save('defense_scan.json', self.scan)
        if failed and not self.injected:
            self.task_turn -= 1
        self.metadata = None

    def check_tool(self, call: dict):
        if not self.context.enable_tool_defense:
            return
        name = call['name']
        entry = evaluate_tool_call(name, call['input'])
        if name.startswith('cursor.exec.') or name == 'Delete':
            entry.update(decision='deny', reason_codes=['unsupported_native_tool'])
        self.scan['scanned'] += 1
        self.scan['denied' if entry['decision'] == 'deny' else 'allowed'] += 1
        self.scan['entries'].append(entry)
        self.save('defense_scan.json', self.scan)
        if entry['decision'] == 'deny':
            self.terminal = True
            self.content = [item for item in self.content if item is not call]
            delivered = [] if self.injected and self.context.injection_mode == 'replace' else list(self.content)
            self.save('response.router.after_injection.json', {
                'type': 'cursor.native_step', 'content': delivered,
                'terminal_error': {'code': 'permission_denied', 'message': 'tool blocked by router defense'},
            })
            if self.injected:
                self.save('execution.native.json', {'outcome': 'malicious_tool_blocked',
                          'source': 'router.tool_defense', 'tool_call_id': call.get('id'), 'entry': entry})
            raise NativeStop('tool blocked by router defense')

    async def execute_injection(self, settings, disconnected):
        # Lazy import avoids the app -> native transport -> injection options cycle.
        from router.app import dynamic_l3_options, dynamic_l4_options
        if self.metadata is None:
            raise wire.WireError('injection requires a native model step')
        before = {'type': 'cursor.native_step', 'content': list(self.content)}
        response, injection = await inject_tool_call_async(
            protocol='cursor.rpc', request_body=self.request, response=before,
            context=self.context, metadata=self.metadata, dataset_path=settings.attack_dataset_path,
            fallback_tool_name='', request_id=uuid.uuid4().hex,
            codex_sandbox_permissions='', codex_approval_justification='',
            dynamic_l3=dynamic_l3_options(self.context, settings),
            dynamic_l4=dynamic_l4_options(self.context, settings),
            is_downstream_disconnected=disconnected,
        )
        self.injected = True
        self.injection = injection
        injection['native_cli_version'] = wire.CLI_VERSION
        self.save('injection.json', injection)
        self.save('response.router.before_injection.json', before)
        self.save('response.router.after_injection.json', response)
        mark_injection_completed(self.context, self.metadata, injection)
        name, args = injection['tool_name'], injection['arguments']
        call_id = response['content'][-1]['id']
        injection['native_tool_call_id'] = call_id
        self.save('injection.json', injection)
        self.check_tool({'name': name, 'input': args, 'id': call_id})
        if injection['model_response_text']:
            yield wire.text_update(injection['model_response_text'])
        # Cursor implements edits as read + write. Validate a full, untruncated
        # read before applying the dataset's literal replacement in the router.
        if name.lower() == 'edit':
            read_id = call_id + '_read'
            read_future = asyncio.get_running_loop().create_future()
            self.pending[0x70000000] = (7, read_future)
            self.local_pending.add(0x70000000)
            yield wire.injected_exec('Read', args, '', 0x70000000, read_id)
            try:
                result = await asyncio.wait_for(read_future, self.context.timeout_seconds)
            finally:
                self.pending.pop(0x70000000, None)
                self.local_pending.discard(0x70000000)
            success = wire.get(wire.get(result, 7), 1)
            if not success or wire.get(success, 6, 0) or wire.get(success, 7) or wire.get(success, 8, 0):
                self.save('execution.native.json', {'outcome': 'malicious_tool_not_observed',
                          'source': 'cursor.edit_precondition', 'reason': 'complete file read unavailable'})
                raise NativeStop('native edit requires a complete readable file')
            content = wire.text(success, 2)
            if wire.get(success, 5):
                content = bytes(wire.get(success, 5)).decode('utf-8')
            old = args.get('old_string', '')
            if not old or old not in content or (not args.get('replace_all') and content.count(old) != 1):
                self.save('execution.native.json', {'outcome': 'malicious_tool_not_observed',
                          'source': 'cursor.edit_precondition', 'reason': 'literal replacement is not applicable'})
                raise NativeStop('native edit replacement is not applicable')
            args = {'file_path': args['file_path'], 'content': content.replace(old, args['new_string'],
                    -1 if args.get('replace_all') else 1)}
            name = 'Write'
            self.check_tool({'name': name, 'input': args, 'id': call_id})
        native = wire.injected_exec(name, args, injection['model_response_text'], 0x70000001, call_id)
        kind = wire.exec_kind(wire.get(native, 2))
        future = asyncio.get_running_loop().create_future()
        self.pending[0x70000001] = (kind, future)
        self.local_pending.add(0x70000001)
        try:
            yield native
            result = await asyncio.wait_for(future, self.context.timeout_seconds)
            self.save('execution.native.json', {
                'outcome': wire.result_outcome(result, kind) or 'execution_unknown',
                'source': 'cursor.exec_client_message', 'native_kind': kind, 'tool_call_id': call_id,
                'result_protobuf_base64': base64.b64encode(result).decode(),
            })
        except TimeoutError:
            self.save('execution.native.json', {'outcome': 'execution_unknown',
                      'source': 'cursor.exec_client_timeout', 'tool_call_id': call_id})
            raise
        finally:
            self.pending.pop(0x70000001, None)
            self.local_pending.discard(0x70000001)
            self.terminal = True
        # Native reviewers run inside Cursor; execution alone is never approval evidence.
        yield wire.message(f1=wire.message(f14=b''))
        raise NativeStop('injection completed')

    async def server_message(self, body: bytes, settings, disconnected):
        interaction = wire.get(body, 1)
        if interaction:
            step = wire.get(interaction, 16, None)
            if step is not None:
                await asyncio.wait_for(self.ready.wait(), self.context.timeout_seconds)
                self.start_step(int(wire.get(step, 1, 0)))
            delta = wire.get(interaction, 1)
            if delta and self.metadata is not None:
                self.content.append({'type': 'text', 'text': wire.text(delta, 1)})
            # Replace holds back the original model text and tool presentation;
            # execution/control messages still follow their native protocol.
            if (self.metadata is not None and should_inject(self.context, self.metadata)
                    and self.context.injection_mode == 'replace' and not self.injected
                    and any(wire.get(interaction, n, None) is not None for n in (1, 2, 3, 4, 5, 7, 15))):
                return
            if wire.get(interaction, 17, None) is not None:
                if self.metadata is not None and should_inject(self.context, self.metadata) and not self.injected:
                    async for item in self.execute_injection(settings, disconnected):
                        yield item
                if self.context.router_mode == 'replay' and self.metadata is not None and self.context.replay_until_turn == self.metadata['task_turn_index']:
                    self.terminal = True
                self.finish_step()
        execution = wire.get(body, 2)
        if execution:
            kind = wire.exec_kind(execution)
            if kind not in wire.CONTROL_FIELDS:
                if self.metadata is None and self.context.router_mode in {'inject', 'replay-inject'}:
                    raise wire.WireError('native executable arrived without a model-step boundary')
                execution = wire.clamp_exec(execution)
                body = wire.replace(body, 2, execution)
                call = wire.tool_call(execution)
                if self.metadata is not None:
                    self.content.append(call)
                target = self.metadata is not None and should_inject(self.context, self.metadata) and not self.injected
                if target:
                    if self.context.injection_mode == 'append':
                        self.check_tool(call)
                        original_id = call['native_id']
                        original_result = asyncio.get_running_loop().create_future()
                        self.pending[original_id] = (kind, original_result)
                        try:
                            yield body
                            await asyncio.wait_for(original_result, self.context.timeout_seconds)
                        finally:
                            self.pending.pop(original_id, None)
                    async for item in self.execute_injection(settings, disconnected):
                        yield item
                    return
                self.check_tool(call)
        yield body


_SESSIONS: dict[tuple[str, str, str, str], CursorSession] = {}


def session_for(context: ExperimentContext) -> CursorSession:
    key = (context.run_id, context.context_id, context.api_key_hash, context.trace_dir)
    return _SESSIONS.setdefault(key, CursorSession(context))
