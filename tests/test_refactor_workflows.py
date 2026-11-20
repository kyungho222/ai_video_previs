import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
from ai_video.video_higgsfield_jutsu.python_backend.video_generation import VideoManager
from ai_video.video_higgsfield_jutsu.python_backend.repositories import local_repositories
from ai_video.video_higgsfield_jutsu.python_backend.remote_tools import call_tool


class SourceListingTests(unittest.TestCase):
    def test_completed_previs_keeps_source_duration_and_prompt(self):
        with TemporaryDirectory() as folder:
            router=create_api_router(root=Path(folder),python_client=SimpleNamespace(),agent=SimpleNamespace())
            job,_,_=router.service.store.create('A performer.',duration=10)
            job['media']=[{'name':'previs.mp4','mediaType':'video/mp4'}]
            router.service.store.finish(job,{'message':'Done.'})
            app=FastAPI();app.include_router(router)
            response=TestClient(app).get('/api/video/sources')
            self.assertEqual(response.status_code,200,response.text)
            media=response.json()['results'][0]['media'][0]
            self.assertEqual(media['sourceDuration'],10)
            self.assertEqual(media['scenePrompt'],'A performer.')


class RemoteRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_failure_does_not_prevent_scheduling(self):
        import asyncio
        with TemporaryDirectory() as folder:
            root=Path(folder)
            repositories=local_repositories(root)
            service=SimpleNamespace(root=root,config={},transport=None,repositories=repositories,
                tasks=set(),usage_history=SimpleNamespace(record_video_source=Mock(side_effect=OSError('locked'))))
            manager=VideoManager(service)
            manager.run=AsyncMock()
            job,_=repositories.video_jobs.create('52345678-1234-1234-1234-123456789abc',
                {'uploadedId':'62345678-1234-1234-1234-123456789abc'},{'provider':'wan'})
            with self.assertLogs('uvicorn.error',level='WARNING'):
                manager.ensure(job)
            await asyncio.gather(*service.tasks)
            manager.run.assert_awaited_once()

    async def test_read_retry_and_mutation_no_replay(self):
        remote=SimpleNamespace(call=AsyncMock(side_effect=[TimeoutError(),{'structuredContent':{'ok':True}}]))
        result=await call_tool(remote,'scene_builder_3d_get_project',{},sleep=AsyncMock())
        self.assertTrue(result['structuredContent']['ok'])
        self.assertEqual(remote.call.await_count,2)
        remote.call=AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            await call_tool(remote,'scene_builder_3d_run_python',{},sleep=AsyncMock())
        self.assertEqual(remote.call.await_count,1)

    async def test_uploaded_mp4_uses_real_inspection_and_retention(self):
        import cv2
        import numpy as np
        with TemporaryDirectory() as folder:
            root=Path(folder)
            sample=root/'sample.mp4'
            writer=cv2.VideoWriter(str(sample),cv2.VideoWriter_fourcc(*'mp4v'),24,(64,64))
            self.assertTrue(writer.isOpened())
            for _ in range(3):writer.write(np.zeros((64,64,3),dtype=np.uint8))
            writer.release()
            repositories=local_repositories(root/'service')
            uploaded=await repositories.uploaded_previs.save(
                UploadFile(filename='sample.mp4',file=io.BytesIO(sample.read_bytes())), 'A sample.')
            metadata,path=repositories.uploaded_previs.get(uploaded['id'])
            self.assertEqual(path.read_bytes(),sample.read_bytes())
            self.assertAlmostEqual(metadata['metadata']['duration'],3/24)
            self.assertEqual(metadata['scenePrompt'],'A sample.')

    async def test_existing_provider_job_publishes_without_resubmitting(self):
        import asyncio
        with TemporaryDirectory() as folder:
            root=Path(folder)
            repositories=local_repositories(root)
            service=SimpleNamespace(root=root,config={},transport=None,repositories=repositories,
                                    archive=repositories.archive,slots=asyncio.Semaphore(1))
            manager=VideoManager(service)
            job_id='32345678-1234-1234-1234-123456789abc'
            repositories.video_jobs.create(job_id,{'uploadedId':'42345678-1234-1234-1234-123456789abc'},
                                           {'provider':'wan','region':'singapore','duration':5,'prompt':'A sample.'})
            repositories.video_jobs.claim(job_id,'owner')
            repositories.video_jobs.update(job_id,owner='owner',state='running',provider_id='provider-task')
            async def download(url,target,**kwargs):
                target.write_bytes(b'\x00\x00\x00\x18ftypisom'+b'\x00'*32)
            adapter=SimpleNamespace(capabilities=SimpleNamespace(provider='wan',model='mock-video'),
                configured=True,poll=AsyncMock(return_value={'task_status':'SUCCEEDED','video_url':'https://example.com/video.mp4',
                                                           '_usage':{'duration':5}}),
                submit=AsyncMock(),download=download)
            manager.adapters['wan']=adapter
            manager._prepare_submission=AsyncMock(side_effect=AssertionError('Must not resubmit existing job'))
            with self.assertLogs("uvicorn.error", level="INFO") as captured:
                await manager.run(job_id,'owner')
            self.assertIn(f"[작업 {job_id}] 영상 생성 완료", "\n".join(captured.output))
            result=repositories.video_jobs.get(job_id)
            self.assertEqual(result['state'],'completed')
            self.assertTrue((root/'runtime/wan'/job_id/'wan.mp4').is_file())
            self.assertEqual(result['metadata']['outputValidation']['status'],'unavailable')
            self.assertEqual(result['metadata']['ai_usage'][0]['quantity'],5)
            adapter.submit.assert_not_awaited()
