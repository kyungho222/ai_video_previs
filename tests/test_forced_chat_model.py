from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
from ai_video.video_higgsfield_jutsu.python_backend.work_settings import video_analysis_model
from ai_video.video_higgsfield_jutsu.python_backend.video_models import VideoCapabilities


@pytest.fixture(autouse=True)
def temporarily_force_model_for_override_regressions(monkeypatch):
    # The production default is now frontend routing; retain coverage of the opt-in override.
    monkeypatch.setattr('ai_video.video_higgsfield_jutsu.python_backend.work_settings.FORCE_DEFAULT_CHAT_MODEL', True)


@pytest.mark.parametrize('requested', ['gpt-4.1-mini', 'external-model', '', None, 123, {'id':'external'}, ['external']])
def test_frontend_model_is_ignored_for_previs_and_filter(tmp_path, requested):
    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    service = router.service
    service.execute = AsyncMock()
    app = FastAPI()
    app.include_router(router)
    with patch('ai_video.video_higgsfield_jutsu.python_backend.app.filter_user_prompts',
               new=AsyncMock(return_value={})) as inspect:
        response = TestClient(app).post('/api/previs/generate', json={
            'message':'성인이 손을 흔든다.', 'model':requested, 'db_name':'tenant',
        })
    assert response.status_code == 202, response.text
    job = service.execute.call_args.args[0]
    assert job['chatModel'] == service.default_model
    assert job['workSettings']['model'] == service.default_model
    assert inspect.await_args.kwargs['model'] == service.default_model


def test_scenario_model_and_persisted_settings_are_forced(tmp_path):
    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    app = FastAPI()
    app.include_router(router)
    with patch('ai_video.video_higgsfield_jutsu.python_backend.shortform_routes.generate_scenario',
               new=AsyncMock(return_value={'scenes':[{'title':'인사','prompt':'성인이 손을 흔든다.'}]})) as generate:
        response = TestClient(app).post('/api/shortform/scenario', json={
            'title':'인사', 'summary':'성인이 인사한다.', 'model':'external-model',
        })
    assert response.status_code == 200, response.text
    assert generate.await_args.kwargs['model'] == router.service.default_model
    saved = router.service.scenarios.get(response.json()['sessionId'])
    assert saved['settings']['model'] == router.service.default_model


def test_video_analysis_ignores_previous_model_settings():
    service = SimpleNamespace(default_model='gpt-6.1-terra', config={'VIDEO_PROMPT_MODEL':'old-analysis-model'})
    assert video_analysis_model(service, {'workSettings':{'model':'old-client-model'}}) == 'gpt-6.1-terra'


def test_old_scenario_model_cannot_override_current_default(tmp_path):
    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    service = router.service
    service.execute = AsyncMock()
    session_id = service.scenarios.save({
        'title':'인사','summary':'인사한다.', 'scenes':[{'title':'인사','prompt':'성인이 손을 흔든다.'}],
        'settings':{'model':'old-client-model','db_name':'tenant','chatbotid':''},
    })
    app = FastAPI()
    app.include_router(router)
    with patch('ai_video.video_higgsfield_jutsu.python_backend.app.filter_user_prompts',
               new=AsyncMock(return_value={})):
        response = TestClient(app).post('/api/previs/generate', headers={'sessionId':session_id}, json={})
    assert response.status_code == 202, response.text
    assert service.execute.call_args.args[0]['chatModel'] == service.default_model


def test_video_request_forces_chat_model_and_keeps_video_provider(tmp_path):
    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    service, manager = router.service, router.video
    service.execute = AsyncMock()
    manager.adapters['fake'] = SimpleNamespace(configured=True, capabilities=VideoCapabilities(
        provider='fake', label='Fake', model='fake-video', regions=('singapore',),
    ))
    manager.source = Mock(return_value={})
    manager.ensure = Mock()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    with patch('ai_video.video_higgsfield_jutsu.python_backend.app.filter_user_prompts',
               new=AsyncMock(return_value={})):
        previs = client.post('/api/previs/generate', json={'message':'성인이 손을 흔든다.'})
    saved = service.store.get(previs.json()['jobId'])['data']
    saved['remoteProjects'] = [str(uuid4())]
    service.store.checkpoint(saved)
    with patch('ai_video.video_higgsfield_jutsu.python_backend.video_routes.filter_user_prompts',
               new=AsyncMock(return_value={})):
        response = client.post('/api/video/generate', json={
            'requestId':str(uuid4()), 'provider':'fake', 'model':'fake-video',
            'source':{'jobId':previs.json()['jobId'],'mediaIndex':0},
            'prompt':'성인이 손을 흔든다.', 'duration':5, 'resolution':'480P',
            'referenceMode':'video', 'chatModel':{'id':'external-model'},
        })
    assert response.status_code == 202, response.text
    assert response.json()['options']['chatModel'] == service.default_model
    assert response.json()['options']['model'] == 'fake-video'
