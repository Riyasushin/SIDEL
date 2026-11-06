"""Offline replay of Cursor RPC recordings, including native tool-result barriers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from router import cursor_wire as wire
from router.context import ExperimentContext
from router.replay import ReplayLookupError


def native_root(trace_source: str) -> Path:
    source = Path(trace_source)
    if source.name == 'native_traces':
        return source
    return source.parent / 'native_traces'


def request_shape(path: str, body: bytes, content_type: str) -> dict:
    raw = wire.unary_body(body, content_type)
    shape: dict = {'path': path}
    if path.endswith('/BidiAppend'):
        request_id, seq, client = wire.append_message(raw)
        kinds = sorted(f.number for f in wire.fields(client))
        shape.update(append_seq=seq, kinds=kinds)
        # Execution IDs and result variants must match the delivered native tool.
        execution = wire.get(client, 2)
        if execution:
            shape.update(exec_id=wire.text(execution, 15), native_id=int(wire.get(execution, 1, 0)),
                         exec_fields=sorted(f.number for f in wire.fields(execution) if f.number not in {1, 15, 39, 45}))
        wire.user_prompt(client)  # reject embedded credentials before recording
    return shape


def load_native_replay(context: ExperimentContext, path: str, ordinal: int,
                       body: bytes, content_type: str) -> tuple[Path, dict, dict]:
    root = native_root(context.trace_source_dir)
    shape = request_shape(path, body, content_type)
    candidates = []
    for metadata_path in root.glob('*/metadata.json'):
        meta = json.loads(metadata_path.read_text())
        if meta.get('rpc_path') != path:
            continue
        # Concurrent BidiAppend calls are ordered by protocol sequence, not HTTP arrival.
        matched = (meta.get('request_shape', {}).get('append_seq') == shape['append_seq']
                   if 'append_seq' in shape else meta.get('rpc_ordinal') == ordinal)
        if matched:
            candidates.append((metadata_path.parent, meta))
    if len(candidates) != 1:
        raise ReplayLookupError(f'native replay requires one source RPC: {path} ordinal={ordinal}; found {len(candidates)}')
    source, meta = candidates[0]
    if meta.get('status') != 'completed' or meta.get('native_cli_version') != wire.CLI_VERSION:
        raise ReplayLookupError('native replay source is incomplete or has an incompatible CLI version')
    if meta.get('agent') != 'cursor' or meta.get('instance_id') != context.instance_id:
        raise ReplayLookupError('native replay source agent/task mismatch')
    expected = (source / 'request.body.bin').read_bytes()
    exact = hashlib.sha256(expected).hexdigest() == hashlib.sha256(body).hexdigest()
    if context.replay_match_mode == 'exact_request_hash':
        # Exact always fails closed, even when replay_strict was disabled for a
        # different transport. Correlation IDs are part of the native request.
        if not exact:
            raise ReplayLookupError(f'strict native replay request hash mismatch: {path}')
    elif shape != meta.get('request_shape'):
        raise ReplayLookupError(f'native replay incompatible tools/client messages: {path}')
    record = {
        'mode': context.router_mode, 'match_mode': context.replay_match_mode,
        'source_turn_dir': str(source), 'request_hash_matched': exact,
        'compatibility_passed': True, 'matched_by': 'native_rpc_sequence',
        'expected_request_hash': hashlib.sha256(expected).hexdigest(),
        'actual_request_hash': hashlib.sha256(body).hexdigest(),
        'native_cli_version': wire.CLI_VERSION,
    }
    return source, meta, record
