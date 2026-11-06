from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx
from router.app import app
from router.config import Settings, get_settings
from router.cursor_native import _TOKENS
from router import cursor_wire as wire
from router.cursor_session import _SESSIONS


class CursorNativeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(_env_file=None, ROUTER_MODE='proxy-record', ROUTER_TRACE_AGENT='cursor',
                                 ROUTER_TRACE_DIR=str(self.root / 'traces'), ROUTER_API_KEY='routing-key',
                                 CURSOR_API_KEY='upstream-account-key', CURSOR_BASE_URL='https://cursor.invalid')
        app.dependency_overrides[get_settings] = lambda: self.settings
        _TOKENS.clear()
        _SESSIONS.clear()
        self.real_client = httpx.AsyncClient
        self.client = self.real_client(transport=httpx.ASGITransport(app=app), base_url='http://router')
        self.requests = []

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        _TOKENS.clear()
        self.temp.cleanup()

    def backend(self, request):
        self.requests.append(request)
        if request.url.path == '/auth/exchange_user_api_key':
            self.assertEqual(request.headers['authorization'], 'Bearer upstream-account-key')
            return httpx.Response(200, json={'accessToken': 'upstream-session-token'})
        self.assertEqual(request.headers['authorization'], 'Bearer upstream-session-token')
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield wire.frame(wire.text_update('recording'))
                yield wire.end_frame()
        return httpx.Response(200, stream=Stream(),
                              headers={'content-type': 'application/connect+proto'})

    def factory(self, **kwargs):
        return self.real_client(transport=httpx.MockTransport(self.backend), **kwargs)

    async def test_authentication_and_stream_recording_keep_account_tokens_private(self):
        with mock.patch('router.cursor_native.httpx.AsyncClient', side_effect=self.factory):
            auth = await self.client.post('/cursor/auth/exchange_user_api_key', headers={'authorization': 'Bearer routing-key'})
            self.assertEqual(auth.status_code, 200)
            token = auth.json()['accessToken']
            self.assertNotIn('upstream', token)
            response = await self.client.post('/cursor/agent.v1.AgentService/RunSSE',
                headers={'authorization': f'Bearer {token}', 'content-type': 'application/connect+proto'}, content=wire.frame(wire.message(f1='run')))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, wire.frame(wire.text_update('recording')) + wire.end_frame())
            self.assertEqual(len(self.requests), 2)
        traces = list((self.root / 'native_traces').glob('*'))
        self.assertEqual(len(traces), 1)
        self.assertEqual((traces[0] / 'request.body.bin').read_bytes(), wire.frame(wire.message(f1='run')))
        self.assertEqual((traces[0] / 'response.body.bin').read_bytes(), response.content)
        metadata = (traces[0] / 'metadata.json').read_text()
        self.assertEqual(json.loads(metadata)['status'], 'completed')
        for secret in ('upstream-account-key', 'upstream-session-token', 'routing-key', token):
            self.assertNotIn(secret, metadata)

    async def test_unknown_key_and_modified_session_token_fail_closed(self):
        with mock.patch('router.cursor_native.httpx.AsyncClient', side_effect=self.factory):
            auth = await self.client.post('/cursor/auth/exchange_user_api_key', headers={'authorization': 'Bearer routing-key'})
            for token in ('', 'wrong', auth.json()['accessToken'] + 'x'):
                response = await self.client.post('/cursor/agent.v1.AgentService/RunSSE', headers={'authorization': f'Bearer {token}'})
                self.assertEqual(response.status_code, 401)
            self.assertEqual(len(self.requests), 1)

    async def test_invalid_native_injection_request_is_rejected_without_backend_calls(self):
        self.settings.router_mode = 'inject'
        with mock.patch('router.cursor_native.httpx.AsyncClient') as upstream:
            response = await self.client.post('/cursor/agent.v1.AgentService/RunSSE', headers={'authorization': 'Bearer routing-key'})
            self.assertEqual(response.status_code, 400)
            upstream.assert_not_called()

    async def test_native_endpoint_rejects_standard_agent_context(self):
        self.settings.router_trace_agent = 'codex'
        response = await self.client.post('/cursor/auth/exchange_user_api_key', headers={'authorization': 'Bearer routing-key'})
        self.assertEqual(response.status_code, 400)
