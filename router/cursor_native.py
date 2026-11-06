"""Cursor native RPC recording, offline replay and response intervention.

Cursor's CLI uses an HTTP/1 SSE fallback for the bidirectional agent service.
Upstream account credentials stay in the router; task containers use routing keys.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

from router.config import Settings
from router.context import ExperimentContext, load_context_registry
from router import cursor_wire as wire
from router.cursor_session import session_for, NativeStop
from router.cursor_replay import load_native_replay, request_shape
from router.replay import ReplayLookupError
from router.tracing import next_turn_index, write_turn_artifact

# Account tokens are short-lived and never persisted in traces.
_TOKENS: dict[tuple[str, str], tuple[float, str]] = {}
_TOKEN_LOCK = asyncio.Lock()


def session_token(context: ExperimentContext, upstream: str) -> str:
    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    # Preserve identity claims used by the native CLI, but never the upstream signature.
    claims = {}
    try:
        raw = upstream.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        if isinstance(payload, dict):
            claims = payload
    except (ValueError, IndexError):
        pass
    claims.update(sidel_context=context.api_key_hash, exp=int(time.time()) + context.timeout_seconds + 300)
    body = encode({'alg': 'HS256', 'typ': 'JWT'}) + '.' + encode(claims)
    signature = hmac.new(context.api_key.encode(), body.encode(), hashlib.sha256).digest()
    return body + '.' + base64.urlsafe_b64encode(signature).decode().rstrip('=')


def resolve_native_context(request: Request, settings: Settings) -> ExperimentContext:
    token = request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    registry = load_context_registry(settings)
    for context in registry.contexts:
        if hmac.compare_digest(token, context.api_key):
            return context
        try:
            body, signature = token.rsplit('.', 1)
            expected = base64.urlsafe_b64encode(hmac.new(context.api_key.encode(), body.encode(), hashlib.sha256).digest()).decode().rstrip('=')
            claims_raw = body.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(claims_raw + '=' * (-len(claims_raw) % 4)))
            if hmac.compare_digest(signature, expected) and claims['sidel_context'] == context.api_key_hash and claims['exp'] > time.time():
                return context
        except (ValueError, KeyError, IndexError, TypeError):
            pass
    raise HTTPException(status_code=401, detail='invalid Cursor routing credential')


async def upstream_token(settings: Settings) -> str:
    if not settings.cursor_api_key:
        raise HTTPException(status_code=503, detail='CURSOR_API_KEY is not configured on the router')
    key = (settings.cursor_base_url, hashlib.sha256(settings.cursor_api_key.encode()).hexdigest())
    async with _TOKEN_LOCK:
        cached = _TOKENS.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        async with httpx.AsyncClient(trust_env=False, timeout=settings.request_timeout_seconds) as client:
            response = await client.post(
                settings.cursor_base_url.rstrip('/') + '/auth/exchange_user_api_key',
                headers={'authorization': f'Bearer {settings.cursor_api_key}'}, json={},
            )
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f'Cursor authentication failed (HTTP {response.status_code})')
        payload = response.json()
        token = payload.get('accessToken')
        if not isinstance(token, str) or not token:
            raise HTTPException(status_code=502, detail='Cursor authentication returned no access token')
        _TOKENS[key] = (time.monotonic() + 240, token)
        return token


async def proxy_cursor(request: Request, path: str, settings: Settings):
    context = resolve_native_context(request, settings)
    if context.agent != 'cursor':
        raise HTTPException(status_code=400, detail='Cursor RPC requires a Cursor context')
    replay = context.router_mode in {'replay', 'replay-inject'}
    if path == 'auth/exchange_user_api_key':
        upstream = '' if replay else await upstream_token(settings)
        return JSONResponse({'accessToken': session_token(context, upstream)})
    if not path.startswith(('aiserver.v1.', 'agent.v1.')) or '..' in path:
        raise HTTPException(status_code=404, detail='unsupported Cursor RPC path')
    if path in {'agent.v1.AgentService/Run', 'agent.v1.AgentService/RunPoll'}:
        raise HTTPException(status_code=400, detail='use the configured Cursor HTTP/1 RunSSE transport')
    session = session_for(context)
    is_stream = path == 'agent.v1.AgentService/RunSSE'
    is_append = path == 'aiserver.v1.BidiService/BidiAppend'
    is_reviewer = path.endswith(('/ClassifyAgentAutoReview', '/ClassifySandAutoReview'))
    review_settings_rpc = path.endswith(('/GetUserSmartAutoSettings', '/GetTeamSmartAutoSettings'))
    if is_stream and (session.active_stream or session.terminal):
        raise HTTPException(status_code=409, detail='native context is already running or terminal')
    body = await request.body()
    content_type = request.headers.get('content-type', 'application/proto')
    if request.headers.get('content-encoding', 'identity') != 'identity':
        raise HTTPException(status_code=415, detail='compressed HTTP request bodies are not supported')
    try:
        raw = wire.unary_body(body, content_type, request.headers.get('connect-content-encoding', ''))
        shape = request_shape(path, body, content_type)
        request_id = ''
        local_result = False
        if is_append:
            request_id, seq, client_message = wire.append_message(raw)
            # The synthetic native tool belongs to the router, not Cursor's
            # upstream agent. Its result is consumed here and never misattributed
            # to the original server-side tool promise.
            result = wire.get(client_message, 2)
            local_result = bool(result and int(wire.get(result, 1, 0)) in session.local_pending)
            if (request_id, seq) in session.inputs:
                if session.inputs[(request_id, seq)] != client_message:
                    raise wire.WireError('conflicting native append retry')
                return Response(b'', media_type='application/proto')
        elif is_stream:
            request_id = wire.text(raw, 1)
            if not request_id:
                raise wire.WireError('native stream has no request ID')
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail=f'invalid Cursor protobuf request: {exc}') from exc

    if is_stream:
        # Reserve before any upstream await. BidiAppend remains concurrent so
        # executor results can arrive while the response stream is waiting.
        if session.active_stream or session.terminal:
            raise HTTPException(status_code=409, detail='native context is already running or terminal')
        session.active_stream = True
    ordinal = session.ordinal(path)
    source = source_meta = replay_record = None
    # Synthetic results have no clean-source counterpart in replay-inject.
    if replay and not local_result and not (is_append and session.terminal) and not (is_reviewer or review_settings_rpc):
        try:
            source, source_meta, replay_record = load_native_replay(context, path, ordinal, body, content_type)
            session.replay_record = replay_record
        except (ReplayLookupError, ValueError, OSError) as exc:
            if is_stream:
                session.active_stream = False
            write_turn_artifact(context, {'turn_index': session.task_turn, 'endpoint': 'cursor.rpc'},
                                'replay.error.json', {'error': str(exc), 'rpc_path': path})
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if is_append:
        await session.observe_client(request_id, seq, client_message)

    trace_dir = Path(context.trace_dir).parent / 'native_traces' / uuid.uuid4().hex
    trace_dir.mkdir(parents=True, exist_ok=False)
    (trace_dir / 'request.body.bin').write_bytes(body)
    metadata = {**context.public_metadata(), 'endpoint': 'cursor.rpc', 'rpc_path': path,
                'rpc_ordinal': ordinal, 'request_shape': shape, 'native_cli_version': wire.CLI_VERSION,
                'content_type': content_type, 'status': 'started'}
    reviewer_metadata = None
    if is_reviewer:
        from datetime import datetime, timezone
        reviewer_metadata = {**context.public_metadata(), 'endpoint': 'cursor.rpc', 'request_role': 'reviewer',
                             'turn_index': next_turn_index(context, 'reviewer'), 'task_turn_index': None,
                             'started_at': datetime.now(timezone.utc).isoformat()}
        review_call_id = wire.text(wire.get(raw, 1), 1)
        write_turn_artifact(context, reviewer_metadata, 'metadata.json', reviewer_metadata)
        write_turn_artifact(context, reviewer_metadata, 'request.agent.json',
                            {'native_tool_call_id': review_call_id, 'rpc_path': path,
                             'injected_tool': bool(session.injection and session.injection.get('native_tool_call_id') == review_call_id),
                             'request_protobuf_base64': base64.b64encode(raw).decode()})
    if replay_record:
        metadata['replay'] = replay_record
    def save_metadata():
        (trace_dir / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    save_metadata()
    if is_append and (local_result or session.terminal):
        (trace_dir / 'response.body.bin').write_bytes(b'')
        metadata.update(status='completed', upstream_status=200, response_content_type='application/proto',
                        response_origin='router.native_executor')
        save_metadata()
        return Response(b'', media_type='application/proto')
    upstream = client = None
    if source is None:
        try:
            token = await upstream_token(settings)
        except BaseException:
            if is_stream:
                session.active_stream = False
            metadata['status'] = 'authentication_error'
            save_metadata()
            raise
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(settings.request_timeout_seconds, read=context.timeout_seconds))
        forwarded = {k: v for k, v in request.headers.items() if k.lower() not in {
            'host', 'authorization', 'cookie', 'content-length', 'connection', 'content-encoding',
            'keep-alive', 'transfer-encoding', 'te', 'trailer', 'upgrade', 'connect-accept-encoding',
            'proxy-authorization', 'proxy-authenticate', 'x-api-key', 'api-key', 'openai-api-key',
        }}
        forwarded.update(authorization=f'Bearer {token}', **{'accept-encoding': 'identity', 'connect-accept-encoding': 'gzip'})
        try:
            upstream = await client.send(client.build_request(request.method,
                settings.cursor_base_url.rstrip('/') + '/' + path, params=request.query_params,
                headers=forwarded, content=body), stream=True)
        except BaseException:
            if is_stream:
                session.active_stream = False
            metadata['status'] = 'transport_error'
            save_metadata()
            await client.aclose()
            raise
        status = upstream.status_code
        response_type = upstream.headers.get('content-type', 'application/proto')
        compression = upstream.headers.get('connect-content-encoding', '')
        if status == 401:
            _TOKENS.pop((settings.cursor_base_url, hashlib.sha256(settings.cursor_api_key.encode()).hexdigest()), None)
    else:
        status = source_meta['upstream_status']
        response_type = source_meta['response_content_type']
        compression = ''
    metadata.update(upstream_status=status, response_content_type=response_type)
    async def input_frames():
        if source is not None:
            # Recorded output is paced by the native append watermark; the
            # connection cannot finish before the CLI submits its tool results.
            # Replaying upstream frames keeps defense/injection distinct from
            # delivered frames in a new experiment.
            filename = ('upstream.frames.jsonl' if context.replay_response_file.startswith('response.upstream')
                        or context.replay_response_file.startswith('response.router.before') else 'response.frames.jsonl')
            for line in (source / filename).read_text().splitlines():
                item = json.loads(line)
                await session.wait_sequence(request_id, item['after_client_seq'])
                yield item['flags'], base64.b64decode(item['data'], validate=True)
            return
        parser = wire.Frames(compression)
        with (trace_dir / 'upstream.body.bin').open('wb') as raw_output:
            async for chunk in upstream.aiter_bytes():
                raw_output.write(chunk)
                for flags, data in parser.feed(chunk):
                    yield flags, data
            parser.finish()

    async def stream():
        terminal_sent = False
        try:
            with (trace_dir / 'response.body.bin').open('wb') as output:
                if not is_stream or status != 200:
                    collected = bytearray()
                    if source is not None:
                        chunks = [(source / 'response.body.bin').read_bytes()]
                        for chunk in chunks:
                            collected.extend(chunk)
                            output.write(chunk)
                            yield chunk
                    else:
                        async for chunk in upstream.aiter_bytes():
                            if len(collected) + len(chunk) > wire.MAX_MESSAGE:
                                raise wire.WireError('native unary response exceeds limit')
                            collected.extend(chunk)
                            output.write(chunk)
                            yield chunk
                    if reviewer_metadata is not None and status == 200:
                        success = wire.get(wire.get(bytes(collected), 1), 1)
                        decision = {1: 'allowed', 2: 'denied'}.get(wire.get(success, 1, 0), 'unknown')
                        write_turn_artifact(context, reviewer_metadata, 'response.router.after_injection.json',
                                            {'decision': decision, 'reason': wire.text(success, 2),
                                             'source': 'cursor.native_classifier'})
                else:
                    if response_type.split(';')[0] != 'application/connect+proto':
                        raise wire.WireError('Cursor RunSSE/RunPoll requires Connect protobuf output')
                    with (trace_dir / 'upstream.frames.jsonl').open('w') as upstream_frames, (trace_dir / 'response.frames.jsonl').open('w') as delivered_frames:
                        def record(target, flags, data):
                            target.write(json.dumps({'flags': flags, 'data': base64.b64encode(data).decode(),
                                         'after_client_seq': session.last_sequence(request_id)}) + '\n')
                            target.flush()
                        async for flags, data in input_frames():
                            record(upstream_frames, flags, data)
                            if flags & 2:
                                terminal_sent = True
                                payload = json.loads(data)
                                if 'error' in payload:
                                    metadata['status'] = 'failed'
                                    session.finish_step(failed=True)
                                record(delivered_frames, flags, data)
                                chunk = wire.frame(data, flags)
                                output.write(chunk)
                                yield chunk
                                break
                            async for delivered in session.server_message(data, settings, request.is_disconnected):
                                record(delivered_frames, 0, delivered)
                                chunk = wire.frame(delivered)
                                output.write(chunk)
                                yield chunk
                    if not terminal_sent:
                        raise wire.WireError('native stream ended without Connect end frame')
            if metadata['status'] != 'failed':
                metadata['status'] = 'completed'
        except NativeStop as exc:
            metadata.update(status='completed', stop_reason=str(exc))
            # Ending the stream releases the upstream tool promise; never forge
            # a successful result for a command the CLI did not execute.
            chunk = wire.end_frame(None if str(exc) == 'injection completed' else 'permission_denied', str(exc))
            with (trace_dir / 'response.body.bin').open('ab') as output:
                output.write(chunk)
            with (trace_dir / 'response.frames.jsonl').open('a') as output:
                output.write(json.dumps({'flags': 2, 'data': base64.b64encode(chunk[5:]).decode(),
                                         'after_client_seq': session.last_sequence(request_id)}) + '\n')
            yield chunk
        except (wire.WireError, TimeoutError, ReplayLookupError, ValueError) as exc:
            metadata.update(status='failed', error=str(exc))
            session.terminal = True
            session.finish_step(failed=True)
            yield wire.end_frame('failed_precondition', 'native router could not complete this step; see router artifacts')
        except BaseException:
            metadata['status'] = 'interrupted'
            session.finish_step(failed=True)
            raise
        finally:
            if is_stream:
                session.active_stream = False
                session.finish_step()
            save_metadata()
            if upstream is not None:
                await upstream.aclose()
            if client is not None:
                await client.aclose()
    return StreamingResponse(stream(), status_code=status, headers={'content-type': response_type})
