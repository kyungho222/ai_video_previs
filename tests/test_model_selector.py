from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.model_gateway import canonical_model, resolve_model
from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router


@pytest.mark.parametrize('value',['unknown-model','',None,123,{'model':'unknown'},'https://untrusted.invalid','gemma'])
def test_unmatched_frontend_model_falls_back_to_current_gpt(tmp_path,value,caplog):
    router=create_api_router(root=tmp_path,python_client=SimpleNamespace(),agent=SimpleNamespace())
    router.service.execute=AsyncMock()
    app=FastAPI(); app.include_router(router)
    with patch('ai_video.video_higgsfield_jutsu.python_backend.app.filter_user_prompts',new=AsyncMock(return_value={})) as inspect:
        result=TestClient(app).post('/api/previs/generate',json={'message':'성인이 손을 흔든다.','model':value})
    assert result.status_code==202,result.text
    assert router.service.execute.call_args.args[0]['chatModel']=='gpt-6.1-terra'
    assert inspect.await_args.kwargs['model']=='gpt-6.1-terra'
    entries=[record.message for record in caplog.records if '[model-routing] fallback' in record.message]
    assert len(entries)==1
    assert 'selected=gpt-6.1-terra' in entries[0]


def test_explicit_default_and_known_alias_do_not_log_fallback(caplog):
    assert canonical_model('gpt-6.1-terra')=='gpt-6.1-terra'
    assert canonical_model('deepseek')=='deepseek-chat'
    assert not any('[model-routing] fallback' in record.message for record in caplog.records)


def test_fallback_log_does_not_echo_arbitrary_input(caplog):
    canonical_model('https://username:private-password@untrusted.invalid/?token=private-token')
    assert '[model-routing] fallback' in caplog.text
    assert 'private-password' not in caplog.text
    assert 'private-token' not in caplog.text


def test_gateway_fallback_and_matched_missing_key_are_distinct():
    route=resolve_model('not-registered',config={'OPENAI_API_KEY':'test-key'})
    assert route.model=='gpt-6.1-terra'
    assert route.provider=='openai'
    with pytest.raises(ValueError,match='DEEPSEEK_API_KEY'):
        resolve_model('deepseek',config={'OPENAI_API_KEY':'test-key'})


def test_model_catalog_exposes_real_option_values(tmp_path):
    router=create_api_router(root=tmp_path,python_client=SimpleNamespace(),agent=SimpleNamespace())
    router.service.config['DEEPSEEK_API_KEY']='test-provider-key'
    app=FastAPI(); app.include_router(router)
    value=TestClient(app).get('/api/chat/models').json()
    assert value['default']=='gpt-6.1-terra'
    options=value['options']
    assert len({item['value'] for item in options})==len(options)
    deepseek=next(item for item in options if item['value']=='deepseek-chat')
    assert deepseek['provider']=='deepseek'
    assert deepseek['configured'] is True
    assert all(item['value']!='deepseek' for item in options)
    assert 'test-provider-key' not in str(value)
