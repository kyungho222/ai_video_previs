import asyncio
import json
import unittest
from unittest.mock import AsyncMock
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.prompt_filter import filter_user_prompts, PromptFilterError
from test_prompt_filter import inspection_response


def rating_response(sexual=0, violence=0):
    value = dict(sexual_level=sexual, violence_level=violence,
                 sexual_reason='Scene context.', violence_reason='Scene context.')
    return {'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(value)}]}]}


class RatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_injection_and_rating_are_started_concurrently(self):
        started = set()
        both_started = asyncio.Event()

        async def request(path, body):
            if path == 'responses':
                name = body['text']['format']['name']
                started.add(name)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 1)
                if name == 'content_rating_admission':
                    return rating_response(sexual=1)
            return inspection_response(path)

        result = await filter_user_prompts('성인 두 사람이 짧게 키스한다.', model='selected', request=request)
        self.assertEqual(result['rating']['overall'], 1)
        self.assertIn('medium or wide', result['generationRules'])
        self.assertIn('brief', result['generationRules'])

    async def test_grade_three_is_blocked(self):
        async def request(path, body):
            if path == 'responses' and body['text']['format']['name'] == 'content_rating_admission':
                return rating_response(violence=3)
            return inspection_response(path)
        with self.assertRaises(PromptFilterError) as raised:
            await filter_user_prompts('A graphic scene.', model='selected', request=request)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn('graphic_violence', raised.exception.categories)

    async def test_grade_two_has_stronger_limits(self):
        async def request(path, body):
            if path == 'responses' and body['text']['format']['name'] == 'content_rating_admission':
                return rating_response(violence=2)
            return inspection_response(path)
        result = await filter_user_prompts('A threatening confrontation.', model='selected', request=request)
        self.assertIn('implied', result['generationRules'])
        self.assertEqual(result['rating']['violence'], 2)

    async def test_failed_rating_returns_inspection_error(self):
        async def request(path, body):
            if path == 'responses' and body['text']['format']['name'] == 'content_rating_admission':
                return rating_response(sexual=4)
            return inspection_response(path)
        with self.assertRaises(PromptFilterError) as raised:
            await filter_user_prompts('A scene.', model='selected', request=request)
        self.assertEqual(raised.exception.status_code, 503)

    async def test_rules_survive_final_prompt_compression(self):
        from ai_video.video_higgsfield_jutsu.python_backend.video_common import fit_video_prompt
        from ai_video.video_higgsfield_jutsu.python_backend.prompt_filter import presentation_rules
        rules = presentation_rules({'sexual':1,'violence':0})
        text = 'A brief kiss in a two-shot. ' * 900 + '\n' + rules
        request = AsyncMock(return_value={'output':[{'content':[{
            'type':'output_text', 'text':json.dumps({'prompt':'Adults share a brief kiss.'}),
        }]}]})
        result = await fit_video_prompt(text, request=request, model='selected', max_length=20000,
                                        required_texts=[rules], context={'generationRules':rules})
        self.assertIn(rules, result['prompt'])
        self.assertLessEqual(len(result['prompt']),20000)

    async def test_minors_moderation_cannot_be_downgraded_by_rating(self):
        async def request(path, body):
            if path == 'responses' and body['text']['format']['name'] == 'content_rating_admission':
                return rating_response(sexual=0)
            return inspection_response(path, **{'sexual/minors':True})
        with self.assertRaises(PromptFilterError) as raised:
            await filter_user_prompts('Unsafe scene.', model='selected', request=request)
        self.assertEqual(raised.exception.status_code,422)

    async def test_cached_rating_is_reused(self):
        async def request(path, body):
            if path == 'responses' and body['text']['format']['name'] == 'content_rating_admission':
                return rating_response(sexual=1)
            return inspection_response(path)
        mocked = AsyncMock(side_effect=request)
        first = await filter_user_prompts('A kiss.', model='selected',request=mocked)
        again = await filter_user_prompts('A kiss.', model='selected',request=mocked,cached=first)
        self.assertEqual(mocked.await_count,3)
        self.assertEqual(first['generationRules'],again['generationRules'])


class RatingAdmissionTests(unittest.TestCase):
    def test_grade_one_is_attached_to_previs_job_before_execution(self):
        from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router

        def respond(request):
            body=json.loads(request.content)
            path='responses' if request.url.path.endswith('/responses') else 'moderations'
            if path=='responses' and body['text']['format']['name']=='content_rating_admission':
                return httpx.Response(200,json=rating_response(sexual=1))
            return httpx.Response(200,json=inspection_response(path))

        with TemporaryDirectory() as folder:
            router=create_api_router(root=Path(folder),python_client=SimpleNamespace(),
                                     agent=SimpleNamespace(run=AsyncMock()),transport=httpx.MockTransport(respond))
            router.service.config['OPENAI_API_KEY']='test'
            router.service.execute=AsyncMock()
            app=FastAPI();app.include_router(router)
            result=TestClient(app).post('/api/chat',json={'message':'키스'})
            self.assertEqual(result.status_code,202,result.text)
            job=router.service.execute.call_args.args[0]
            self.assertEqual(job['promptFilter']['rating']['overall'],1)
            self.assertIn('medium or wide',job['promptFilter']['generationRules'])

    def test_inherited_high_grade_is_blocked_before_new_job(self):
        from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
        from test_prompt_filter import http_inspection_response
        with TemporaryDirectory() as folder:
            router=create_api_router(root=Path(folder),python_client=SimpleNamespace(),
                agent=SimpleNamespace(run=AsyncMock()),transport=httpx.MockTransport(http_inspection_response))
            router.service.config['OPENAI_API_KEY']='test'
            baseline,session,_=router.service.store.create('포르노 장면')
            baseline['codeOperations']=[{'code':'scene'}]
            baseline['media']=[{'mediaType':'video/mp4'}]
            router.service.store.finish(baseline,{'message':'done'})
            router.service.store.create=Mock()
            app=FastAPI();app.include_router(router)
            result=TestClient(app).post('/api/chat',json={'message':'카메라만 수정','sessionId':session})
            self.assertEqual(result.status_code,422,result.text)
            router.service.store.create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
