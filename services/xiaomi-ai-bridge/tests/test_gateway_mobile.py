"""Real loopback HTTP tests; no production state or upstream connections."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from xiaomi_ai_bridge.health import start_health_server


class MobileGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth_required = False
        self.bridge = SimpleNamespace(
            models=SimpleNamespace(
                authorized=lambda value: not self.auth_required or value == 'Bearer fixture-key',
                resolve=Mock(side_effect=ValueError('fixture model is not configured')),
                model_settings=Mock(return_value={}),
            ),
            public_models=Mock(return_value={'providers':[
                {'enabled':True,'models':['fixture-a','fixture-b']},
                {'enabled':False,'models':['disabled-model']},
            ]}),
            update_connection_config=Mock(),
            model_usage=SimpleNamespace(record_request=Mock()),
        )
        with patch('xiaomi_ai_bridge.health.web.TCPSite') as site:
            site.return_value.start = AsyncMock()
            self.runner = await start_health_server(SimpleNamespace(), self.bridge, SimpleNamespace(), SimpleNamespace(), '127.0.0.1', 0)
        self.client = TestClient(TestServer(self.runner.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.runner.cleanup)
        self.addAsyncCleanup(self.client.close)

    async def test_model_schema_and_common_url_variants(self):
        for path in ['/v1/models','/v1/models/','/models','/models/']:
            with self.subTest(path=path):
                response = await self.client.get(path, headers={'Authorization':'Bearer anything'})
                self.assertEqual(response.status,200)
                data = await response.json()
                self.assertEqual(data['object'],'list')
                self.assertEqual([m['id'] for m in data['data']],['fixture-a','fixture-b'])
                for model in data['data']:
                    self.assertEqual(model['object'],'model')
                    self.assertIs(type(model['created']),int)
                    self.assertIsInstance(model['owned_by'],str)

    async def test_webview_preflight_accepts_authorization_and_json(self):
        for path,method in [('/v1/models','GET'),('/models','GET'),('/v1/chat/completions','POST'),('/v1/messages','POST'),('/v1/responses','POST')]:
            with self.subTest(path=path):
                response = await self.client.options(path,headers={
                    'Origin':'capacitor://localhost',
                    'Access-Control-Request-Method':method,
                    'Access-Control-Request-Headers':'authorization,content-type,x-stainless-lang',
                    'Access-Control-Request-Private-Network':'true',
                })
                self.assertEqual(response.status,204)
                self.assertEqual(response.headers['Access-Control-Allow-Origin'],'*')
                self.assertIn('authorization',response.headers['Access-Control-Allow-Headers'])
                self.assertIn(method,response.headers['Access-Control-Allow-Methods'])
                self.assertEqual(response.headers['Access-Control-Allow-Private-Network'],'true')
                self.assertNotIn('Access-Control-Allow-Credentials',response.headers)
        self.bridge.models.resolve.assert_not_called()

    async def test_anonymous_and_webview_model_gets_work_in_no_auth_mode(self):
        for headers in [{}, {'Origin':'null'}, {'Origin':'https://client.example','Authorization':'Bearer 123'}]:
            with self.subTest(headers=headers):
                response = await self.client.get('/v1/models',headers=headers)
                self.assertEqual(response.status,200)
                if headers.get('Origin'):
                    self.assertEqual(response.headers['Access-Control-Allow-Origin'],'*')

    async def test_auth_required_still_protects_all_aliases_and_errors_have_cors(self):
        self.auth_required = True
        for path in ['/v1/models','/models/']:
            response = await self.client.get(path,headers={'Origin':'https://client.example','Authorization':'Bearer wrong'})
            self.assertEqual(response.status,401)
            self.assertEqual(response.headers['Access-Control-Allow-Origin'],'*')
        response = await self.client.get('/v1/models',headers={'Authorization':'Bearer fixture-key'})
        self.assertEqual(response.status,200)
        response = await self.client.post('/v1/chat/completions',json={},headers={'Origin':'https://client.example'})
        self.assertEqual(response.status,401)

    async def test_cross_origin_gateway_post_reaches_own_validation(self):
        response = await self.client.post('/v1/chat/completions',json={'model':'missing'},headers={'Origin':'capacitor://localhost'})
        self.assertEqual(response.status,400)
        self.assertIn('fixture model', (await response.json())['error']['message'])
        self.assertEqual(response.headers['Access-Control-Allow-Origin'],'*')
        self.bridge.models.resolve.assert_called_once()

    async def test_management_routes_do_not_gain_cors_or_cross_origin_writes(self):
        response = await self.client.put('/api/config',json={},headers={'Origin':'https://client.example'})
        self.assertEqual(response.status,403)
        self.assertNotIn('Access-Control-Allow-Origin',response.headers)
        self.bridge.update_connection_config.assert_not_called()
        response = await self.client.options('/api/models',headers={'Origin':'https://client.example','Access-Control-Request-Method':'PUT'})
        self.assertEqual(response.status,405)
        self.assertNotIn('Access-Control-Allow-Origin',response.headers)

    async def test_preflight_does_not_allow_arbitrary_methods(self):
        response = await self.client.options('/v1/models',headers={'Origin':'null','Access-Control-Request-Method':'DELETE'})
        self.assertEqual(response.status,405)

    async def test_sse_headers_are_set_before_stream_is_prepared(self):
        self.bridge.models.resolve.side_effect = None
        self.bridge.models.resolve.return_value = ({'id':'fixture','protocol':'openai','base_url':'http://mock.invalid/v1','api_key':'dummy'},'fixture-a')
        class Content:
            async def iter_chunked(self,_):
                yield b'data: {"id":"fixture"}\n\n'
                yield b'data: [DONE]\n\n'
        class Response:
            status=200
            headers={'Content-Type':'text/event-stream'}
            content=Content()
            def release(self):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
        class Session:
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def post(self,*_,**__):return Response()
        with patch('xiaomi_ai_bridge.health.aiohttp.ClientSession',return_value=Session()):
            response = await self.client.post('/v1/chat/completions',json={'model':'fixture-a','messages':[{'role':'user','content':'hello'}],'stream':True},headers={'Origin':'capacitor://localhost'})
            self.assertEqual(response.status,200)
            self.assertEqual(response.headers['Access-Control-Allow-Origin'],'*')
            self.assertIn('[DONE]',await response.text())
