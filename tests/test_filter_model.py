import unittest
from unittest.mock import AsyncMock
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ai_video.video_higgsfield_jutsu.python_backend.prompt_filter import filter_user_prompts
from test_prompt_filter import inspection_response


class FilterModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_model_is_used_and_model_changes_invalidate_cache(self):
        request = AsyncMock(side_effect=lambda path, body: inspection_response(path, name=body.get('text',{}).get('format',{}).get('name','')))
        first = await filter_user_prompts('Five dancers.', model='selected-model-a', request=request)
        self.assertEqual(request.call_args_list[0].args[1]['model'], 'selected-model-a')
        self.assertEqual(first['injectionModel'], 'selected-model-a')
        await filter_user_prompts('Five dancers.', model='selected-model-a', request=request, cached=first)
        self.assertEqual(request.await_count, 3)
        await filter_user_prompts('Five dancers.', model='selected-model-b', request=request, cached=first)
        self.assertEqual(request.await_count, 6)
        self.assertEqual(request.call_args_list[3].args[1]['model'], 'selected-model-b')


class FilterModelEndpointTests(unittest.TestCase):
    def test_chat_uses_frontend_model_and_falls_back_for_unregistered_models(self):
        from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
        captured = []

        def respond(request):
            path = 'responses' if request.url.path.endswith('/responses') else 'moderations'
            if path == 'responses':
                captured.append(json.loads(request.content)['model'])
            name = json.loads(request.content).get('text',{}).get('format',{}).get('name','')
            return httpx.Response(200, json=inspection_response(path, name=name))

        with TemporaryDirectory() as folder:
            router = create_api_router(root=Path(folder), python_client=SimpleNamespace(),
                                       agent=SimpleNamespace(run=AsyncMock()), transport=httpx.MockTransport(respond))
            router.service.config['OPENAI_API_KEY'] = 'test'
            router.service.chat_models = ['gpt-6.1-terra', 'gpt-4.1']
            router.service.execute = AsyncMock()
            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)
            result = client.post('/api/chat', json={'message':'Five dancers.', 'model':'gpt-4.1'})
            self.assertEqual(result.status_code, 202, result.text)
            self.assertEqual(captured, ['gpt-4.1'] * 2)
            rejected = client.post('/api/chat', json={'message':'Five dancers.', 'model':'unregistered'})
            self.assertEqual(rejected.status_code, 202)
            self.assertEqual(captured, ['gpt-4.1'] * 2 + ['gpt-6.1-terra'] * 2)


if __name__ == '__main__':
    unittest.main()


class MalformedDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_echoed_schema_is_repaired_by_same_model(self):
        calls=[]
        async def request(path,body):
            name=body.get('text',{}).get('format',{}).get('name','')
            calls.append((name,body.get('model')))
            if name=='prompt_injection_admission' and sum(n==name for n,m in calls)==1:
                return {'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(body['text']['format']['schema'])}]}]}
            return inspection_response(path,name=name)
        result=await filter_user_prompts('Two people wave.',model='gemma',request=request,allow_fallback=False)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(sum(n=='prompt_injection_admission' for n,m in calls),2)
        self.assertTrue(all(m=='gemma' for n,m in calls if n))

    async def test_persistent_schema_echo_is_never_admitted(self):
        async def request(path,body):
            name=body.get('text',{}).get('format',{}).get('name','')
            if name=='prompt_injection_admission':
                return {'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(body['text']['format']['schema'])}]}]}
            return inspection_response(path,name=name)
        from ai_video.video_higgsfield_jutsu.python_backend.prompt_filter import PromptFilterError
        with self.assertRaises(PromptFilterError):
            await filter_user_prompts('Two people wave.',model='gemma',request=request,allow_fallback=False)
