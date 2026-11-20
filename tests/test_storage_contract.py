import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from ai_video.video_higgsfield_jutsu.python_backend.repositories import local_repositories
from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router


class StorageContractTests(unittest.TestCase):
    def test_runtime_move_preserves_existing_archive_records(self):
        with TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            feature = project / "ai_video/video_higgsfield_jutsu"
            legacy = local_repositories(feature)
            media = legacy.archive.directory / "saved.mp4"
            media.write_bytes(b"saved media")
            record = legacy.archive.track("saved", media)
            runtime = feature.parent / "runtime"
            (feature / "runtime").rename(runtime)

            with patch("ai_video.video_higgsfield_jutsu.python_backend.repositories.FEATURE_ROOT", feature):
                router = create_api_router(
                    root=feature, python_client=SimpleNamespace(), agent=SimpleNamespace()
                )
            self.assertEqual(router.service.archive.path("saved"), runtime / "archive/saved.mp4")
            self.assertEqual(router.service.archive.record("saved")["expiresAt"], record["expiresAt"])
            self.assertEqual(router.service.usage_history.path, runtime / "archive/usage-history.sqlite3")
            self.assertEqual(router.video.previs.directory, runtime / "wan-inputs")
            self.assertEqual(router.video.root, feature.parent)
            self.assertFalse((feature / "runtime").exists())

    def test_app_and_video_manager_use_injected_repositories(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            repositories = local_repositories(root)
            router = create_api_router(root=root, python_client=SimpleNamespace(),
                                       agent=SimpleNamespace(), repositories=repositories)
            self.assertIs(router.service.store, repositories.previs_jobs)
            self.assertIs(router.service.archive, repositories.archive)
            self.assertIs(router.service.usage_history, repositories.history)
            self.assertIs(router.video.store, repositories.video_jobs)
            self.assertIs(router.video.assets, repositories.assets)
            self.assertIs(router.video.previs, repositories.uploaded_previs)
            self.assertEqual(repositories.video_jobs.path, root / 'runtime/data/wan-jobs.sqlite3')
            self.assertEqual(repositories.archive.database, root.resolve() / 'runtime/data/media-archive.sqlite3')
            self.assertEqual(repositories.assets.root, root / 'runtime/data/wan-assets')

    def test_history_reads_repository_methods_without_sql_access(self):
        with TemporaryDirectory() as folder:
            repositories = local_repositories(Path(folder))
            self.assertEqual(repositories.previs_jobs.history_snapshot(), ([], []))
            self.assertEqual(repositories.video_jobs.for_source('missing'), [])
            self.assertEqual(repositories.video_jobs.all_jobs(), [])
