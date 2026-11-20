import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
import json
import os
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.usage_history import UsageHistory
from ai_video.video_higgsfield_jutsu.python_backend.store import JobStore
from ai_video.video_higgsfield_jutsu.python_backend.video_common import VideoJobs
from ai_video.video_higgsfield_jutsu.python_backend.media_archive import MediaArchive
from ai_video.video_higgsfield_jutsu.python_backend.ai_usage import COLUMNS

PREVIS='12345678-1234-1234-1234-123456789abc'
VIDEO='22345678-1234-1234-1234-123456789abc'


class UsageHistoryTests(unittest.TestCase):
    def test_history_survives_reopen_and_redacts_sensitive_usage(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'history.sqlite3'
            history=UsageHistory(path)
            history.save_previs({'id':PREVIS,'request':'Five dancers.','scenePrompt':'A stage.'},'completed')
            history.record_usage(PREVIS,{'id':'call-1','job_id':PREVIS,'input_tokens':42,
                'raw_usage':{'input_tokens':42,'access_token':'secret-value'}})
            history.record_usage(PREVIS,{'id':'call-1','job_id':PREVIS,'input_tokens':42,
                'raw_usage':{'input_tokens':42,'access_token':'secret-value'}})
            restored=UsageHistory(path)
            self.assertEqual(restored.get(PREVIS)['request'],'Five dancers.')
            rows=restored.usage(PREVIS)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['input_tokens'],42)
            self.assertEqual(set(rows[0]),set(COLUMNS))
            self.assertIsNone(rows[0]['cost_usd'])
            self.assertNotIn('secret-value',str(rows))
            from ai_video.video_higgsfield_jutsu.python_backend.store import redact
            nested = redact({'ai_usage': rows}, usage=True)['ai_usage'][0]
            self.assertEqual(nested['input_tokens'], 42)
            self.assertIsNone(nested['output_tokens'])
            self.assertEqual(nested['raw_usage']['input_tokens'], 42)

    def test_videos_join_to_source_job_without_triggering_generation(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            history=UsageHistory(root/'history.sqlite3')
            service=SimpleNamespace(root=root,store=JobStore(),archive=MediaArchive(root))
            manager=SimpleNamespace(store=VideoJobs(root/'videos.sqlite3'),ensure=Mock(),previs=SimpleNamespace())
            history.save_previs({'id':PREVIS,'request':'Five dancers.'},'completed')
            manager.store.create(VIDEO,{'jobId':PREVIS,'mediaIndex':0},{'prompt':'Render the performance.'})
            manager.store.update(VIDEO,state='completed')
            rows=history.list_records(service,manager)
            self.assertEqual(rows['items'][0]['jobId'],PREVIS)
            self.assertEqual(rows['items'][0]['videos'][0]['id'],VIDEO)
            self.assertNotIn('ai_usage',rows['items'][0])
            details=history.details(PREVIS,service,manager)
            self.assertEqual(details['videos'][0]['prompt'],'Render the performance.')
            manager.ensure.assert_not_called()

    def test_old_archive_prompts_and_unindexed_media_are_imported_permanently(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            archive=root/'runtime/archive'
            archive.mkdir(parents=True)
            scene=archive/('scene-'+PREVIS+'.json')
            scene.write_text(json.dumps({'request':'Old request.','scenePrompt':'Old stage.'}))
            media=archive/(PREVIS+'-0.mp4');media.write_bytes(b'old')
            os.utime(media,(1,1))
            history=UsageHistory(archive/'usage-history.sqlite3')
            service=SimpleNamespace(root=root,store=JobStore(),archive=MediaArchive(root))
            manager=SimpleNamespace(store=VideoJobs(root/'videos.sqlite3'))
            listing=history.list_records(service,manager)
            self.assertEqual(listing['items'][0]['request'],'Old request.')
            self.assertEqual(len(listing['items'][0]['previs']),1)
            self.assertIsNone(listing['items'][0]['previs'][0]['expiresAt'])
            self.assertEqual(history.details(PREVIS,service,manager)['ai_usage'],[])

    def test_history_endpoints_only_read_and_validate_pagination(self):
        from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
        with TemporaryDirectory() as folder:
            router=create_api_router(root=Path(folder),python_client=SimpleNamespace(),agent=SimpleNamespace())
            router.service.usage_history.save_previs({'id':PREVIS,'request':'Five dancers.'},'completed')
            router.video.ensure=Mock()
            app=FastAPI();app.include_router(router)
            client=TestClient(app)
            listing=client.get('/api/usage-history')
            self.assertEqual(listing.status_code,200,listing.text)
            self.assertNotIn('ai_usage',listing.json()['items'][0])
            details=client.get('/api/usage-history/'+PREVIS)
            self.assertEqual(details.status_code,200,details.text)
            self.assertEqual(details.json()['columns'],list(COLUMNS))
            self.assertEqual(client.get('/api/usage-history?limit=1000').status_code,422)
            self.assertEqual(client.get('/api/usage-history/not-a-job').status_code,404)
            removed=client.delete('/api/usage-history/'+PREVIS)
            self.assertEqual(removed.status_code,200,removed.text)
            self.assertEqual(client.get('/api/usage-history/'+PREVIS).status_code,404)
            self.assertEqual(client.get('/api/usage-history').json()['items'],[])
            self.assertEqual(client.delete('/api/usage-history/'+PREVIS).status_code,200)
            self.assertEqual(client.delete('/api/usage-history/not-a-job').status_code,404)
            router.video.ensure.assert_not_called()

    def test_deleted_history_stays_deleted_after_sync_and_reopen(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            archive=root/'runtime/archive'
            archive.mkdir(parents=True)
            scene=archive/('scene-'+PREVIS+'.json')
            scene.write_text(json.dumps({'request':'Archived request.'}))
            media=archive/(PREVIS+'-0.mp4')
            media.write_bytes(b'video')
            history=UsageHistory(archive/'usage-history.sqlite3')
            service=SimpleNamespace(root=root,store=JobStore(),archive=MediaArchive(root))
            manager=SimpleNamespace(store=VideoJobs(root/'videos.sqlite3'))
            manager.store.create(VIDEO,{'jobId':PREVIS,'mediaIndex':0},{'prompt':'Final video.'})
            history.list_records(service,manager)
            history.record_usage(PREVIS,{'id':'call-1','input_tokens':42})
            history.delete(PREVIS)
            history.record_usage(PREVIS,{'id':'call-1','input_tokens':42})
            reopened=UsageHistory(history.path)
            self.assertEqual(reopened.list_records(service,manager)['items'],[])
            self.assertIsNone(reopened.details(PREVIS,service,manager))
            self.assertEqual(reopened.usage(PREVIS),[])
            self.assertTrue(media.exists())
            self.assertTrue(scene.exists())
            self.assertIsNotNone(manager.store.get(VIDEO))

    def test_cross_origin_delete_preflight(self):
        from service_app import create_app
        with TemporaryDirectory() as folder:
            app=create_app(previs_root=Path(folder),python_client=SimpleNamespace(),agent=SimpleNamespace())
            response=TestClient(app).options('/api/usage-history/'+PREVIS,headers={
                'Origin':'https://php8.han.kr','Access-Control-Request-Method':'DELETE'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertIn('DELETE',response.headers['access-control-allow-methods'])

    def test_hidden_history_survives_sync_and_can_be_restored(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            history=UsageHistory(root/'history.sqlite3')
            service=SimpleNamespace(root=root,store=JobStore(),archive=MediaArchive(root))
            manager=SimpleNamespace(store=VideoJobs(root/'videos.sqlite3'))
            history.save_previs({'id':PREVIS,'request':'Keep this prompt.'},'completed')
            history.record_usage(PREVIS,{'id':'call-1','input_tokens':42})
            self.assertTrue(history.set_hidden(PREVIS,True))
            reopened=UsageHistory(history.path)
            self.assertEqual(reopened.list_records(service,manager)['items'],[])
            hidden=reopened.list_records(service,manager,hidden=True)['items']
            self.assertEqual(hidden[0]['jobId'],PREVIS)
            self.assertTrue(hidden[0]['hidden'])
            self.assertEqual(reopened.details(PREVIS,service,manager)['ai_usage'][0]['input_tokens'],42)
            self.assertTrue(reopened.set_hidden(PREVIS,False))
            self.assertEqual(len(reopened.list_records(service,manager)['items']),1)
            self.assertEqual(reopened.list_records(service,manager,hidden=True)['items'],[])
            reopened.set_hidden(PREVIS,True)
            reopened.delete(PREVIS)
            self.assertFalse(reopened.set_hidden(PREVIS,False))
            self.assertEqual(reopened.list_records(service,manager,hidden=True)['items'],[])

    def test_visibility_endpoint_validates_input(self):
        from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
        with TemporaryDirectory() as folder:
            router=create_api_router(root=Path(folder),python_client=SimpleNamespace(),agent=SimpleNamespace())
            router.service.usage_history.save_previs({'id':PREVIS,'request':'Keep this prompt.'},'completed')
            app=FastAPI();app.include_router(router)
            client=TestClient(app)
            path='/api/usage-history/'+PREVIS+'/visibility'
            self.assertEqual(client.post(path,json={'hidden':True}).status_code,200)
            self.assertEqual(client.get('/api/usage-history').json()['items'],[])
            self.assertEqual(len(client.get('/api/usage-history?hidden=true').json()['items']),1)
            self.assertEqual(client.post(path,json={'hidden':'false'}).status_code,422)
            self.assertEqual(client.post(path,content='{').status_code,422)
            self.assertEqual(client.post(path,json={'hidden':False}).status_code,200)
            self.assertEqual(len(client.get('/api/usage-history').json()['items']),1)
            client.delete('/api/usage-history/'+PREVIS)
            self.assertEqual(client.post(path,json={'hidden':False}).status_code,404)

    def test_reads_import_legacy_once_then_use_source_queries(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            history=UsageHistory(root/'history.sqlite3')
            service=SimpleNamespace(root=root,store=JobStore(),archive=MediaArchive(root))
            jobs=VideoJobs(root/'videos.sqlite3')
            manager=SimpleNamespace(store=jobs)
            history.save_previs({'id':PREVIS,'request':'Initial.'},'completed')
            history.list_records(service,manager)
            jobs.all_jobs=Mock(side_effect=AssertionError('Repeated full scan'))
            service.store.history_snapshot=Mock(side_effect=AssertionError('Repeated chat scan'))
            jobs.create(VIDEO,{'jobId':PREVIS},{'prompt':'New final video.'})
            jobs.update(VIDEO,state='completed')
            self.assertEqual(history.list_records(service,manager)['items'][0]['videos'][0]['id'],VIDEO)
            self.assertEqual(history.details(PREVIS,service,manager)['videos'][0]['job_id'],VIDEO)


    def test_generated_videos_only_include_successful_jobs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            history = UsageHistory(root/'history.sqlite3')
            service = SimpleNamespace(root=root, store=JobStore(), archive=MediaArchive(root))
            jobs = VideoJobs(root/'videos.sqlite3')
            manager = SimpleNamespace(store=jobs)
            history.save_previs({'id': PREVIS, 'request': 'A performance.'}, 'completed')
            successful_ids = []
            states = []
            for index, state in enumerate(('preparing', 'running', 'failed', 'completed', 'completed')):
                identifier = f'{index + 3}2345678-1234-1234-1234-123456789abc'
                jobs.create(identifier, {'jobId': PREVIS, 'mediaIndex': index}, {'prompt': state})
                jobs.update(identifier, state='completed')
                states.append((identifier, state))
                if state == 'completed':
                    successful_ids.append(identifier)
            for identifier, state in states:
                jobs.update(identifier, state=state)
            for hidden in (False, True):
                history.set_hidden(PREVIS, hidden)
                record = history.list_records(service, manager, hidden=hidden)['items'][0]
                self.assertEqual({v['id'] for v in record['videos']}, set(successful_ids))
                self.assertTrue(all(v['state'] == 'completed' for v in record['videos']))

    def test_failed_only_jobs_leave_generated_videos_empty(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            history = UsageHistory(root/'history.sqlite3')
            service = SimpleNamespace(root=root, store=JobStore(), archive=MediaArchive(root))
            jobs = VideoJobs(root/'videos.sqlite3')
            jobs.create(VIDEO, {'jobId': PREVIS}, {'prompt': 'A performance.'})
            jobs.update(VIDEO, state='failed')
            history.save_previs({'id': PREVIS, 'request': 'A performance.'}, 'completed')
            listing = history.list_records(service, SimpleNamespace(store=jobs))
            self.assertEqual(listing['items'][0]['videos'], [])


if __name__=='__main__':
    unittest.main()
