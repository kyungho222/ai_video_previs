import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
from ai_video.video_higgsfield_jutsu.python_backend.media_archive import MediaArchive
from local_support.archive import FileArchive


@pytest.mark.parametrize('archive_type',[MediaArchive,FileArchive])
def test_new_media_has_no_expiry_and_cleanup_never_deletes_it(tmp_path,archive_type):
    archive=archive_type(tmp_path)
    for directory in ('archive','wan-inputs'):
        path=tmp_path/'runtime'/directory/'old.mp4'
        path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'old media')
        item=archive.track(directory,path,created=100)
        assert item['expiresAt'] is None
        assert archive.path(directory,now=10**12)==path
    assert archive.cleanup(now=10**12)==0
    assert (tmp_path/'runtime/archive/old.mp4').is_file()


@pytest.mark.parametrize('archive_type',[MediaArchive,FileArchive])
def test_existing_expired_media_can_be_made_permanent(tmp_path,archive_type):
    archive=archive_type(tmp_path)
    path=tmp_path/'runtime/archive'/f'{uuid4()}-0.mp4'
    key=path.stem
    path.parent.mkdir(parents=True); path.write_bytes(b'existing')
    archive.track(key,path,created=100)
    if isinstance(archive,FileArchive):
        with archive.state.transaction() as state:
            state['assets'][key]['expiresAt']=200
    else:
        with archive.db() as db:
            db.execute('UPDATE media_assets SET expires=200 WHERE key=?',(key,))
    assert archive.cleanup(now=1000000)==0
    assert path.is_file()
    result=archive.make_permanent()
    assert result['filesUpdated']==1
    assert archive.record(key)['expiresAt'] is None
    assert archive.path(key,now=10**12)==path
    assert archive.make_permanent()['filesUpdated']==0


@pytest.mark.parametrize('mode',['server','local-files'])
def test_history_button_api_converts_all_files_and_job_metadata(tmp_path,monkeypatch,mode):
    monkeypatch.setenv('PREVIS_STORAGE_MODE',mode)
    router=create_api_router(root=tmp_path,python_client=SimpleNamespace(),agent=SimpleNamespace())
    service=router.service
    identifier=str(uuid4())
    path=tmp_path/'runtime/wan'/identifier/'wan.mp4'
    path.parent.mkdir(parents=True); path.write_bytes(b'video')
    key='wan-'+identifier
    service.archive.track(key,path,created=100)
    if mode=='local-files':
        with service.archive.state.transaction() as state:
            state['assets'][key]['expiresAt']=200
    else:
        with service.archive.db() as db:
            db.execute('UPDATE media_assets SET expires=200 WHERE key=?',(key,))
    video=router.video.store.create(identifier,{'jobId':str(uuid4()),'mediaIndex':0},
                                   {'prompt':'A scene.','duration':5,'resolution':'480P'})
    router.video.store.update(identifier,state='completed',metadata={'expiresAt':200,'completedAt':100,'preserve':'value'})
    app=FastAPI();app.include_router(router)
    client=TestClient(app)
    before=client.get('/api/usage-history?limit=20').json()
    assert before['items'][0]['videos'][0]['videoUrl'] is None
    forbidden=client.post('/api/usage-history/retention/permanent',headers={'Origin':'https://untrusted.invalid'})
    assert forbidden.status_code==403
    result=client.post('/api/usage-history/retention/permanent')
    assert result.status_code==200,result.text
    assert result.json()['filesUpdated']==1
    assert result.json()['videoJobsUpdated']==1
    assert result.json()['permanent'] is True
    assert service.archive.path(key,now=10**12).is_file()
    metadata=router.video.store.get(identifier)['metadata']
    assert metadata['expiresAt'] is None and metadata['preserve']=='value'
    listing=client.get('/api/usage-history?limit=20').json()
    video_url=listing['items'][0]['videos'][0]['videoUrl']
    assert video_url==f'/api/video/jobs/{identifier}/video'
    assert client.get(video_url).status_code==200
    again=client.post('/api/usage-history/retention/permanent')
    assert again.status_code==200
    assert again.json()['filesUpdated']==0 and again.json()['videoJobsUpdated']==0


def test_archive_complete_has_no_expiry(tmp_path):
    archive=MediaArchive(tmp_path)
    identifier=str(uuid4())
    path=tmp_path/'runtime/archive'/f'{identifier}-0.mp4'
    path.parent.mkdir(parents=True);path.write_bytes(b'media')
    item={'archive':archive.track(identifier+'-0',path,created=100)}
    result=archive.complete(identifier,[item],completed=200)
    assert result[0]['archive']['expiresAt'] is None


@pytest.mark.parametrize('mode',['server','local-files'])
def test_new_video_result_is_published_permanently(tmp_path,monkeypatch,mode):
    from ai_video.video_higgsfield_jutsu.python_backend.repositories import local_repositories
    monkeypatch.setenv('PREVIS_STORAGE_MODE',mode)
    store=local_repositories(tmp_path).video_jobs
    identifier=str(uuid4())
    store.create(identifier,{'jobId':str(uuid4()),'mediaIndex':0},{'prompt':'Scene','duration':5,'resolution':'480P'})
    assert store.claim(identifier,'owner')
    assert store.update(identifier,owner='owner',state='downloading')
    candidate=tmp_path/'candidate.mp4';candidate.write_bytes(b'media')
    target=tmp_path/'video.mp4'
    assert store.publish_result(identifier,'owner',candidate,target)
    assert store.get(identifier)['metadata']['expiresAt'] is None
