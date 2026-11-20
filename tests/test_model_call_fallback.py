import asyncio
import json
from functools import wraps

import httpx
import pytest
from unittest.mock import AsyncMock

from ai_video.model_gateway import routed_post
from ai_video.video_higgsfield_jutsu.python_backend.prompt_filter import filter_user_prompts, PromptFilterError
from ai_video.video_higgsfield_jutsu.python_backend.model_client import request_openai


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def gpt_response(text='ok'):
    return {'status':'completed','output':[{'type':'message','role':'assistant','content':[
        {'type':'output_text','text':text}]}], 'usage':{'input_tokens':2,'output_tokens':3,'total_tokens':5}}


@pytest.mark.parametrize('status',[400,401,403,404,429,500,503])
@run_async
async def test_provider_http_failure_falls_back_once(status,caplog):
    calls=[]
    def respond(request):
        calls.append(request)
        if request.url.host=='api.moonshot.ai':
            return httpx.Response(status,json={'error':{'type':'invalid_request_error','message':'private body'}})
        return httpx.Response(200,json=gpt_response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result=await routed_post(client,'gpt-key','responses',{'model':'kimi','instructions':'preserve','input':[]},
                                 config={'MOONSHOT_API_KEY':'kimi-key'})
    assert len(calls)==2
    assert json.loads(calls[1].content)['model']=='gpt-6.1-terra'
    assert json.loads(calls[1].content)['instructions']=='preserve'
    assert calls[1].headers['Authorization']=='Bearer gpt-key'
    assert result.extensions['model_route']=={'provider':'openai','model':'gpt-6.1-terra'}
    assert 'fallback' in caplog.text and 'HTTP='+str(status) in caplog.text
    assert 'private body' not in caplog.text and 'kimi-key' not in caplog.text


@pytest.mark.parametrize('kind',['connection','invalid_json','configuration','malformed_body'])
@run_async
async def test_non_http_call_failures_use_gpt(kind):
    calls=[]
    def respond(request):
        calls.append(request)
        if request.url.host=='api.moonshot.ai':
            if kind=='connection':
                raise httpx.ConnectError('private connection details',request=request)
            if kind=='malformed_body':
                return httpx.Response(200,json={'choices':[{'message':None}]})
            return httpx.Response(200,text='not-json')
        return httpx.Response(200,json=gpt_response())
    config={} if kind=='configuration' else {'MOONSHOT_API_KEY':'kimi-key'}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result=await routed_post(client,'gpt-key','responses',{'model':'kimi','input':[]},config=config)
    assert result.status_code==200
    assert result.extensions['model_route']['model']=='gpt-6.1-terra'
    assert len(calls)==(1 if kind=='configuration' else 2)


@run_async
async def test_failed_gpt_fallback_does_not_loop():
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:(calls.append(r) or httpx.Response(503,json={'error':{'code':'unavailable'}})))) as client:
        result=await routed_post(client,'gpt-key','responses',{'model':'kimi','input':[]},config={'MOONSHOT_API_KEY':'kimi-key'})
    assert result.status_code==503
    assert len(calls)==2


@run_async
async def test_provider_safety_rejection_never_retries_with_another_model():
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:(calls.append(r) or httpx.Response(400,json={'error':{'code':'content_policy_violation'}})))) as client:
        result=await routed_post(client,'gpt-key','responses',{'model':'kimi','input':[]},config={'MOONSHOT_API_KEY':'kimi-key'})
    assert result.status_code==400
    assert len(calls)==1


@run_async
async def test_fallback_preserves_content_filter_blocking(monkeypatch):
    monkeypatch.setenv('MOONSHOT_API_KEY','kimi-key')
    def respond(request):
        body=json.loads(request.content)
        if request.url.host=='api.moonshot.ai':
            return httpx.Response(400,json={'error':{'code':'invalid_request'}})
        if request.url.path.endswith('/moderations'):
            return httpx.Response(200,json={'results':[{'categories':{
                'sexual':False,'sexual/minors':False,'violence/graphic':False}}]})
        name=body['text']['format']['name']
        value=({'injection':False,'category':'none'} if name=='prompt_injection_admission' else
               {'sexual_level':3,'violence_level':0,'sexual_reason':'prohibited','violence_reason':'ordinary'})
        return httpx.Response(200,json=gpt_response(json.dumps(value)))
    with pytest.raises(PromptFilterError) as error:
        await filter_user_prompts('A scene.',model='kimi',api_key='gpt-key',transport=httpx.MockTransport(respond))
    assert error.value.status_code==422


@run_async
async def test_usage_reports_fallback_model(monkeypatch):
    monkeypatch.setenv('MOONSHOT_API_KEY','kimi-key')
    rows=[]
    def respond(request):
        return (httpx.Response(401,json={'error':{'code':'invalid_key'}}) if request.url.host=='api.moonshot.ai'
                else httpx.Response(200,json=gpt_response()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await request_openai(client,'gpt-key','responses',{'model':'kimi','input':[]},lambda *a:None,usage_sink=rows.append)
    assert rows[-1]['provider']=='openai' and rows[-1]['model']=='gpt-6.1-terra'


@run_async
async def test_outer_retries_stay_on_gpt(monkeypatch):
    monkeypatch.setenv('MOONSHOT_API_KEY','kimi-key')
    hosts=[]
    def respond(request):
        hosts.append(request.url.host)
        return httpx.Response(503,json={'error':{'code':'unavailable'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError):
            await request_openai(client,'gpt-key','responses',{'model':'kimi','input':[]},lambda *a:None,sleep=AsyncMock())
    assert hosts.count('api.moonshot.ai')==1
    assert hosts.count('api.openai.com')==3


@run_async
async def test_filter_records_actual_fallback_model_and_reuses_valid_cache(monkeypatch):
    monkeypatch.setenv('MOONSHOT_API_KEY','kimi-key')
    calls=[]
    def respond(request):
        calls.append(request)
        body=json.loads(request.content)
        if request.url.host=='api.moonshot.ai':
            return httpx.Response(400,json={'error':{'code':'invalid_request'}})
        if request.url.path.endswith('/moderations'):
            return httpx.Response(200,json={'results':[{'categories':{
                'sexual':False,'sexual/minors':False,'violence/graphic':False}}]})
        name=body['text']['format']['name']
        value=({'injection':False,'category':'none'} if name=='prompt_injection_admission' else
               {'sexual_level':0,'violence_level':0,'sexual_reason':'ordinary','violence_reason':'ordinary'})
        return httpx.Response(200,json=gpt_response(json.dumps(value)))
    kwargs={'model':'kimi','api_key':'gpt-key','transport':httpx.MockTransport(respond)}
    result=await filter_user_prompts('A clean scene.',**kwargs)
    assert result['requestedModel']=='kimi'
    assert result['injectionModel']=='gpt-6.1-terra'
    assert result['ratingModel']=='gpt-6.1-terra'
    count=len(calls)
    cached=await filter_user_prompts('A clean scene.',**kwargs,cached=result)
    assert cached==result and len(calls)==count


@run_async
async def test_chat_finish_reason_content_filter_never_falls_back():
    hosts=[]
    def respond(request):
        hosts.append(request.url.host)
        return httpx.Response(200,json={'choices':[{'finish_reason':'content_filter','message':{'content':''}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result=await routed_post(client,'gpt-key','responses',{'model':'kimi','input':[]},config={'MOONSHOT_API_KEY':'kimi-key'})
    assert hosts==['api.moonshot.ai']
    assert result.json()['incomplete_details']['reason']=='content_filter'


@run_async
async def test_gpt_transport_failure_does_not_restart_original_provider(monkeypatch):
    monkeypatch.setenv('MOONSHOT_API_KEY','kimi-key')
    hosts=[]
    def respond(request):
        hosts.append(request.url.host)
        if request.url.host=='api.openai.com':
            raise httpx.ConnectError('private error',request=request)
        return httpx.Response(503,json={'error':{'code':'unavailable'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError):
            await request_openai(client,'gpt-key','responses',{'model':'kimi','input':[]},lambda *a:None,sleep=AsyncMock())
    assert hosts.count('api.moonshot.ai')==1
    assert hosts.count('api.openai.com')==3


@pytest.mark.parametrize('first_error',['http','transport'])
@run_async
async def test_retry_success_keeps_actual_fallback_model_metadata(monkeypatch,first_error):
    monkeypatch.setenv('MOONSHOT_API_KEY','kimi-key')
    gpt_calls=[]
    def respond(request):
        if request.url.host=='api.moonshot.ai':
            return httpx.Response(503,json={'error':{'code':'unavailable'}})
        gpt_calls.append(request)
        if len(gpt_calls)==1:
            if first_error=='transport':
                raise httpx.ConnectError('connection failed',request=request)
            return httpx.Response(503,json={'error':{'code':'unavailable'}})
        return httpx.Response(200,json=gpt_response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result=await request_openai(client,'gpt-key','responses',{'model':'kimi','input':[]},lambda *a:None,sleep=AsyncMock())
    assert result['_model_route']=={'provider':'openai','model':'gpt-6.1-terra'}
