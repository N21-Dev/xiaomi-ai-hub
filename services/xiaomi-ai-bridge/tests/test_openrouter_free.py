from __future__ import annotations
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaomi_ai_bridge.model_registry import ModelRegistry
from xiaomi_ai_bridge.openrouter_policy import free_catalog_model, enforce_free_request


class FreePolicyTests(unittest.TestCase):
    def test_catalog_requires_free_id_and_all_prices_zero(self):
        self.assertTrue(free_catalog_model({'id':'x/y:free','pricing':{'prompt':'0','completion':'0','request':'0'}}))
        for model in [
            {'id':'x/y','pricing':{'prompt':'0','completion':'0'}},
            {'id':'x/y:free','pricing':{'prompt':'0','completion':'0.1'}},
            {'id':'x/y:free','pricing':{'prompt':'0','completion':'0','image':'0.01'}},
            {'id':'x/y:free','pricing':{'prompt':'0'}},
            {'id':'x/y:free','pricing':{'prompt':None,'completion':'0'}},
            {'id':'x/y:free','pricing':{'prompt':'NaN','completion':'0'}},
        ]:
            self.assertFalse(free_catalog_model(model))

    def test_forwarding_keeps_zero_price_and_blocks_paid_fallbacks(self):
        provider={'free_only':True,'base_url':'https://openrouter.ai/api/v1'}
        body={'model':'x/y:free','provider':{'max_price':{'prompt':10}}}
        result=enforce_free_request(provider,body)
        self.assertEqual(result['provider']['max_price'],{'prompt':0,'completion':0})
        self.assertEqual(body['provider']['max_price']['prompt'],10)
        for changes in [{'model':'paid/model'}, {'models':['paid/model']}, {'plugins':[{'id':'web'}]}, {'preset':'premium'}]:
            with self.assertRaises(ValueError): enforce_free_request(provider,{**body,**changes})
        normal={'model':'paid/model'}
        self.assertIs(enforce_free_request({'free_only':False},normal), normal)


class RegistryFreeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store=ModelRegistry(Path(tmp.name),SimpleNamespace(ai_base_url='',ai_model='',ai_api_key=''))
        await self.store.upsert({'id':'or-free','name':'Free','protocol':'openai','base_url':'https://openrouter.ai/api/v1',
                                 'models':[{'id':'test/a:free','max_output_tokens':2048}], 'free_only':True})

    async def test_public_and_edit_preserve_free_policy_and_reject_paid_models(self):
        self.assertTrue(self.store.public()['providers'][0]['free_only'])
        await self.store.upsert({'name':'Edited'},'or-free')
        self.assertTrue(self.store.public()['providers'][0]['free_only'])
        before=self.store.path.read_bytes()
        with self.assertRaises(ValueError):await self.store.add_models('or-free',['paid/model'])
        with self.assertRaises(ValueError):await self.store.upsert({'models':['paid/model']},'or-free')
        self.assertEqual(before,self.store.path.read_bytes())

    async def test_discovery_filters_paid_models_and_preserves_settings(self):
        payload={'data':[
            {'id':'test/a:free','pricing':{'prompt':'0','completion':'0'}},
            {'id':'test/b:free','pricing':{'prompt':'0','completion':'0'}},
            {'id':'test/paid','pricing':{'prompt':'0.1','completion':'0.1'}},
            {'id':'test/fee:free','pricing':{'prompt':'0','completion':'0','request':'1'}}]}
        class Body:
            async def iter_chunked(self,_):yield json.dumps(payload).encode()
        class Response:
            status=200
            content=Body()
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
        class Session:
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            def get(self,*_,**__):return Response()
        with patch('xiaomi_ai_bridge.model_registry.aiohttp.ClientSession',return_value=Session()):
            result=await self.store.discover('or-free')
        self.assertEqual(result['providers'][0]['models'],['test/a:free','test/b:free'])
        self.assertEqual(result['providers'][0]['model_settings'][0]['max_output_tokens'],2048)
        self.assertEqual(result['discovery']['fetched'],2)
        self.assertEqual(result['active'],{'provider_id':'','model':''})
