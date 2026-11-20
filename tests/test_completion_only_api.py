from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router


def build_client(tmp_path):
    router=create_api_router(root=tmp_path,python_client=SimpleNamespace(),agent=SimpleNamespace())
    job,session,_=router.service.store.create('A scene.')
    app=FastAPI();app.include_router(router)
    return TestClient(app),router,job


def test_running_job_has_no_json_response(tmp_path):
    client,router,job=build_client(tmp_path)
    response=client.get(f'/api/jobs/{job["id"]}?waitSeconds=0')
    assert response.status_code==204
    assert response.content==b''
    assert response.headers.get('retry-after')=='1'
    assert router.service.store.get(job['id'])['state']=='running'


def test_only_final_result_and_usage_are_returned(tmp_path):
    client,router,job=build_client(tmp_path)
    router.service.store.emit(job['id'],'response',{'state':'running'})
    router.service.store.emit(job['id'],'ai_usage',{'model':'gpt-6.1-terra','total_tokens':12})
    router.service.store.finish(job,{'message':'Done','media':[]})
    response=client.get(f'/api/jobs/{job["id"]}')
    assert response.status_code==200
    payload=response.json()
    assert payload['state']=='completed'
    assert [event['type'] for event in payload['events']]==['complete']
    assert payload['ai_usage']==[{'model':'gpt-6.1-terra','total_tokens':12}]
    assert '"running"' not in response.text


def test_failed_result_is_preserved(tmp_path):
    client,router,job=build_client(tmp_path)
    router.service.store.finish(job,error='Render failed')
    response=client.get(f'/api/jobs/{job["id"]}')
    assert response.json()['state']=='failed'
    assert response.json()['events'][-1]['type']=='error'


def test_legacy_progress_mode_is_explicit(tmp_path):
    client,router,job=build_client(tmp_path)
    response=client.get(f'/api/jobs/{job["id"]}?completeOnly=false')
    assert response.status_code==200
    assert response.json()['state']=='running'


def test_missing_job_is_404_instead_of_waiting(tmp_path):
    client,router,job=build_client(tmp_path)
    assert client.get(f'/api/jobs/{uuid4()}?waitSeconds=0').status_code==404


def test_request_waits_and_returns_completion(tmp_path):
    client,router,job=build_client(tmp_path)
    get=router.service.store.get
    reads=[]
    def complete_on_second_read(identifier,after=-1):
        reads.append(after)
        if len(reads)==2:
            router.service.store.finish(job,{'message':'Done','media':[]})
        return get(identifier,after)
    router.service.store.get=complete_on_second_read
    response=client.get(f'/api/jobs/{job["id"]}?waitSeconds=2')
    assert response.status_code==200
    assert response.json()['state']=='completed'


def test_cursor_cannot_hide_final_completion(tmp_path):
    client,router,job=build_client(tmp_path)
    router.service.store.finish(job,{'message':'Done','media':[]})
    result=client.get(f'/api/jobs/{job["id"]}?after=999999').json()
    assert result['state']=='completed'
    assert result['events'][-1]['type']=='complete'


def test_disconnected_wait_does_not_cancel_generation(tmp_path):
    client,router,job=build_client(tmp_path)
    endpoint=next(route.endpoint for route in router.routes if route.path=='/api/jobs/{job_id}')
    request=SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    response=asyncio.run(endpoint(job['id'],request,after=-1,complete_only=True,wait_seconds=25))
    assert response.status_code==204
    assert router.service.store.get(job['id'])['state']=='running'
    router.service.store.finish(job,{'message':'Done','media':[]})
    assert client.get(f'/api/jobs/{job["id"]}').json()['state']=='completed'
