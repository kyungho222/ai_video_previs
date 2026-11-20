from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.media_archive import MediaArchive, RETENTION_SECONDS
from ai_video.video_higgsfield_jutsu.python_backend.video_generation import create_video_router


class SpatialGuideImageTests(unittest.TestCase):
    job_id = '12345678-1234-1234-1234-123456789abc'

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.archive = MediaArchive(root)
        self.path = root / 'runtime' / 'wan' / self.job_id / 'guides' / 'spatial-0.png'
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'guide image')
        self.created = time.time()
        service = SimpleNamespace(root=root, archive=self.archive, config={}, transport=None)
        router = create_video_router(service)
        router.manager.store = SimpleNamespace(get=lambda job_id: {
            'created': self.created,
            'metadata': {'spatialGuides': {'guides': [{'index': 0, 'name': self.path.name}]}},
        })
        app = FastAPI()
        app.include_router(router)
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))
        self.url = f'/api/video/jobs/{self.job_id}/guides/0'
        self.guide_key = f'guide-{self.job_id}-guides-{self.path.name}'
        self.legacy_key = f'spatial-{self.job_id}-0'

    def test_reads_image_registered_by_generation(self):
        self.archive.track(self.guide_key, self.path, created=self.created)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'guide image')

    def test_keeps_expiration_of_legacy_registration(self):
        self.archive.track(self.legacy_key, self.path, created=self.created - RETENTION_SECONDS)
        self.assertEqual(self.client.get(self.url).status_code, 410)

    def test_missing_image_returns_not_found(self):
        self.path.unlink()
        self.assertEqual(self.client.get(self.url).status_code, 404)
