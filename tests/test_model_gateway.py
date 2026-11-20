import json
import asyncio
from functools import wraps
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.model_gateway import available_models, resolve_model, routed_post
from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
from ai_video.video_higgsfield_jutsu.python_backend.model_client import request_openai


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.mark.parametrize('alias,provider,model,key_name,host', [
    ('deepseek','deepseek','deepseek-chat','DEEPSEEK_API_KEY','api.deepseek.com'),
    ('gemini','google','gemini-2.5-flash','GOOGLE_API_KEY','generativelanguage.googleapis.com'),
    ('claude','anthropic','claude-3-5-haiku-latest','ANTHROPIC_API_KEY','api.anthropic.com'),
    ('qwen','qwen','qwen3-max','DASHSCOPE_API_KEY','dashscope-intl.aliyuncs.com'),
    ('grok','xai','grok-3-fast-beta','XAI_API_KEY','api.x.ai'),
    ('mistral','mistral','mistral-large-latest','MISTRAL_API_KEY','api.mistral.ai'),
    ('kimi','moonshot','kimi-k2-0905-preview','MOONSHOT_API_KEY','api.moonshot.ai'),
])
def test_alias_uses_own_key_and_api(alias,provider,model,key_name,host):
    route = resolve_model(alias, config={key_name:'provider-key','OPENAI_API_KEY':'other-key'})
    assert (route.provider,route.model,route.api_key) == (provider,model,'provider-key')
    assert host in route.base_url
    assert 'provider-key' not in repr(route)


def test_missing_provider_key_never_falls_back_to_openai():
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        resolve_model('deepseek',config={'OPENAI_API_KEY':'wrong-provider-key'})


def test_model_catalog_and_disabled_alias():
    assert {'deepseek','qwen','gpt-4.1'}.issubset(available_models())
    assert resolve_model('gemma',config={'OPENAI_API_KEY':'test-key'}).model == 'gpt-6.1-terra'
    assert resolve_model('https://untrusted.invalid',config={'OPENAI_API_KEY':'test-key'}).model == 'gpt-6.1-terra'


@run_async
async def test_compatible_payload_tools_images_schema_and_usage():
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'id':'reply','choices':[{'finish_reason':'tool_calls','message':{
            'content':None,'tool_calls':[{'id':'call-1','type':'function','function':{'name':'build','arguments':'{}'}}],
        }}], 'usage':{'prompt_tokens':12,'completion_tokens':7,'total_tokens':19}})
    body={'model':'qwen','instructions':'Use tools safely.', 'input':[
        {'role':'user','content':[{'type':'input_text','text':'scene'},
                                 {'type':'input_image','image_url':'data:image/png;base64,AA=='}]},
        {'type':'function_call','call_id':'old-call','name':'inspect','arguments':'{}'},
        {'type':'function_call_output','call_id':'old-call','output':'ok'},
    ], 'tools':[{'type':'function','name':'build','description':'Build','parameters':{'type':'object'}}],
    'text':{'format':{'type':'json_schema','name':'result','strict':True,'schema':{'type':'object'}}},
    'max_output_tokens':1000}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response=await routed_post(client,'unused','responses',body,config={'DASHSCOPE_API_KEY':'qwen-key'})
    payload=json.loads(calls[0].content)
    assert str(calls[0].url).endswith('/chat/completions')
    assert calls[0].headers['Authorization']=='Bearer qwen-key'
    assert payload['model']=='qwen3-max'
    assert payload['enable_thinking'] is False
    assert payload['messages'][1]['content'][1]['type']=='image_url'
    assert payload['messages'][2]['tool_calls'][0]['id']=='old-call'
    assert payload['messages'][3]['tool_call_id']=='old-call'
    assert payload['tools'][0]['function']['name']=='build'
    assert payload['response_format']['json_schema']['name']=='result'
    data=response.json()
    assert data['output'][0]['type']=='function_call'
    assert data['output'][0]['call_id']=='call-1'
    assert data['usage']['input_tokens']==12
    assert data['usage']['output_tokens']==7


@run_async
async def test_director_developer_instructions_use_compatible_system_role():
    calls=[]
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await routed_post(client,'unused','responses',{'model':'deepseek','input':[
            {'role':'developer','content':'Follow the 3D workflow and tool permissions.'},
            {'role':'user','content':'Build a scene.'},
        ]},config={'DEEPSEEK_API_KEY':'provider-key'})
    assert calls[0]['messages'][0] == {'role':'system','content':'Follow the 3D workflow and tool permissions.'}
    assert all(item['role'] != 'developer' for item in calls[0]['messages'])


@run_async
async def test_openai_alias_and_moderation_keep_native_protocol():
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:(calls.append(r) or httpx.Response(200,json={'ok':True})))) as client:
        await routed_post(client,'openai-key','responses',{'model':'gpt-5-chat-latest','input':[]},config={})
        await routed_post(client,'openai-key','moderations',{'model':'omni-moderation-latest','input':'scene'},config={})
    assert str(calls[0].url)=='https://api.openai.com/v1/responses'
    assert json.loads(calls[0].content)['model']=='gpt-5.1'
    assert str(calls[1].url)=='https://api.openai.com/v1/moderations'


@run_async
async def test_usage_reports_selected_provider(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-provider-key')
    rows=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':'ok'}}],
        'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5},
    }))) as client:
        result=await request_openai(client,'openai-key','responses',{'model':'deepseek','input':[]},lambda *a:None,usage_sink=rows.append)
    assert result['output_text']=='ok'
    assert rows[0]['provider']=='deepseek'
    assert rows[0]['model']=='deepseek-chat'


def test_frontend_alias_is_accepted_without_default_model_override(tmp_path):
    router=create_api_router(root=tmp_path,python_client=SimpleNamespace(),agent=SimpleNamespace())
    service=router.service
    service.execute=AsyncMock()
    app=FastAPI()
    app.include_router(router)
    with patch('ai_video.video_higgsfield_jutsu.python_backend.app.filter_user_prompts',new=AsyncMock(return_value={})):
        response=TestClient(app).post('/api/previs/generate',json={'message':'성인이 손을 흔든다.','model':'deepseek'})
    assert response.status_code==202,response.text
    assert service.execute.call_args.args[0]['chatModel']=='deepseek-chat'


def test_scenario_uses_selected_provider_end_to_end_with_mock_api(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','deepseek-test-key')
    monkeypatch.setenv('OPENAI_API_KEY','moderation-test-key')
    calls=[]
    scenes=[{'title':'시작','prompt':'창가에 선 성인'},{'title':'인사','prompt':'손을 흔든다.'},
            {'title':'끝','prompt':'웃으며 마무리한다.'}]
    def respond(request):
        calls.append(request)
        body=json.loads(request.content)
        if request.url.host=='api.openai.com':
            assert request.url.path=='/v1/moderations'
            return httpx.Response(200,json={'results':[{'categories':{
                'sexual':False,'sexual/minors':False,'violence/graphic':False}}]})
        assert request.url.host=='api.deepseek.com'
        assert request.headers['Authorization']=='Bearer deepseek-test-key'
        assert body['model']=='deepseek-chat'
        system='\n'.join(item.get('content','') for item in body['messages'] if item['role']=='system')
        if '"injection"' in system:
            value={'injection':False,'category':'none'}
        elif '"sexual_level"' in system:
            value={'sexual_level':0,'violence_level':0,'sexual_reason':'ordinary','violence_reason':'ordinary'}
        else:
            value={'scenes':scenes}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}],
                                      'usage':{'prompt_tokens':3,'completion_tokens':4,'total_tokens':7}})
    router=create_api_router(root=tmp_path,python_client=SimpleNamespace(),agent=SimpleNamespace(),
                             transport=httpx.MockTransport(respond))
    app=FastAPI()
    app.include_router(router)
    result=TestClient(app).post('/api/shortform/scenario',json={
        'title':'인사','summary':'창가에서 손을 흔든다.','model':'deepseek'})
    assert result.status_code==200,result.text
    assert len(calls)==4
    assert result.json()['scenes']==scenes
    assert result.json()['ai_usage'][-1]['provider']=='deepseek'
    assert result.json()['ai_usage'][-1]['total_tokens']==7
