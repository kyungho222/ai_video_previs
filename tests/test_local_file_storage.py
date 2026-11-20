import sys
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import uuid4

STAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE))
sys.path.insert(1, 'C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs')
from local_support.repositories import file_repositories


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.block_db = patch('sqlite3.connect', side_effect=AssertionError('Local mode opened SQLite'))
        self.block_db.start()
        self.bundle = file_repositories(self.root)

    def tearDown(self):
        self.block_db.stop()
        self.temp.cleanup()

    def test_previs_version_and_restart(self):
        store = self.bundle.previs_jobs
        job, session, _ = store.create('dance')
        store.emit(job['id'], 'ai_usage', {'id': 'usage', 'input_tokens': 12})
        job['codeOperations'] = [{'code': 'build'}]
        job['media'] = [{'mediaType': 'video/mp4'}]
        store.finish(job, {'message': 'done'})
        restored = file_repositories(self.root)
        self.assertEqual(restored.previs_jobs.get(job['id'])['state'], 'completed')
        new, returned, _ = restored.previs_jobs.create('bigger camera', session)
        self.assertEqual(returned, session)
        self.assertEqual(new['version'], 2)
        self.assertEqual(new['baseVersion'], 1)
        self.assertIn('dance', new['scenePrompt'])

    def test_video_idempotency_source_lock_and_ownership(self):
        store = self.bundle.video_jobs
        identifier = str(uuid4())
        source = {'jobId': str(uuid4())}
        row, created = store.create(identifier, source, {})
        self.assertTrue(created)
        self.assertFalse(store.create(identifier, source, {})[1])
        with self.assertRaises(ValueError):
            store.create(str(uuid4()), source, {})
        self.assertTrue(store.claim(identifier, 'owner'))
        self.assertFalse(store.claim(identifier, 'other'))
        self.assertFalse(store.update(identifier, owner='other', state='preparing'))
        self.assertTrue(store.update(identifier, owner='owner', state='preparing'))
        self.assertTrue(store.begin_submission(identifier, 'owner'))
        self.assertFalse(store.begin_submission(identifier, 'owner'))
        store.update(identifier, owner='owner', provider_id='remote-id', state='running')
        restored = file_repositories(self.root).video_jobs
        self.assertEqual(restored.get(identifier)['provider_id'], 'remote-id')
        self.assertEqual(len(restored.for_source(source['jobId'])), 1)

    def test_history_hide_delete_and_success_only(self):
        identifier = str(uuid4())
        history = self.bundle.history
        history.save_previs({'id': identifier, 'request': 'dance'}, 'completed')
        history.record_usage(identifier, {'id': 'usage', 'input_tokens': 12})
        store = self.bundle.video_jobs
        bad = str(uuid4())
        good = str(uuid4())
        store.create(bad, {'jobId': identifier}, {})
        store.update(bad, state='failed')
        store.create(good, {'jobId': identifier}, {})
        store.update(good, state='completed')
        service = SimpleNamespace(root=self.root, store=self.bundle.previs_jobs, archive=self.bundle.archive)
        manager = SimpleNamespace(store=store)
        rows = history.list_records(service, manager)['items']
        self.assertEqual([row['id'] for row in rows[0]['videos']], [good])
        history.set_hidden(identifier, True)
        self.assertEqual(history.list_records(service, manager)['items'], [])
        self.assertEqual(len(history.list_records(service, manager, hidden=True)['items']), 1)
        history.delete(identifier)
        history.save_previs({'id': identifier}, 'completed')
        self.assertIsNone(history.get(identifier))
        self.assertEqual(history.usage(identifier), [])
        self.assertIsNone(file_repositories(self.root).history.get(identifier))

    def test_archive_tracks_retention_across_restart(self):
        path = self.root / 'runtime/archive/test.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'video')
        archive = self.bundle.archive
        record = archive.track('test', path, created=100)
        again = file_repositories(self.root).archive
        self.assertEqual(again.record('test')['savedAt'], 100)
        self.assertEqual(again.track('test', path, created=200)['expiresAt'], record['expiresAt'])
        self.assertIsNone(record['expiresAt'])
        self.assertEqual(again.cleanup(now=10**12), 0)
        self.assertTrue(path.exists())

    def test_no_sqlite_files_created(self):
        self.assertEqual(list(self.root.rglob('*.sqlite3')), [])

    def test_concurrent_creates_keep_one_active_source(self):
        source = {'jobId': str(uuid4())}
        def create(_):
            try:
                return self.bundle.video_jobs.create(str(uuid4()), source, {})[1]
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(create, range(16))), 1)
        self.assertEqual(len(file_repositories(self.root).video_jobs.all_jobs()), 1)

    def test_write_failure_rolls_back_memory_and_file(self):
        from local_support.state import JsonState
        value = JsonState(self.root / 'state.json', {'items': []})
        with value.transaction() as state:
            state['items'].append('original')
        with patch.object(Path, 'replace', side_effect=OSError('write failure')):
            with self.assertRaises(OSError):
                with value.transaction() as state:
                    state['items'].append('lost')
        self.assertEqual(value.value['items'], ['original'])
        self.assertEqual(JsonState(value.path, {'items': []}).value['items'], ['original'])

    def test_active_previs_is_not_replayed_after_restart(self):
        job, session, _ = self.bundle.previs_jobs.create('dance')
        self.bundle.previs_jobs.checkpoint({**job, 'remoteProjects': ['project']})
        with self.bundle.previs_jobs.state.transaction() as state:
            state['jobs'][job['id']]['lease'] = 0
        restored = file_repositories(self.root)
        row = restored.previs_jobs.get(job['id'])
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['data']['remoteProjects'], ['project'])
        self.assertEqual(restored.previs_jobs.create('new', session)[0]['version'], 2)

    def test_two_repository_instances_do_not_overwrite_each_other(self):
        other = file_repositories(self.root)
        first = str(uuid4())
        second = str(uuid4())
        self.bundle.video_jobs.create(first, {'jobId': 'first'}, {})
        other.video_jobs.create(second, {'jobId': 'second'}, {})
        self.assertEqual({row['id'] for row in self.bundle.video_jobs.all_jobs()}, {first, second})

    def test_second_console_keeps_live_previs_running(self):
        job, _, _ = self.bundle.previs_jobs.create('dance')
        other = file_repositories(self.root)
        self.assertEqual(other.previs_jobs.get(job['id'])['state'], 'running')

    def test_expired_session_can_accept_a_new_prompt_after_restart(self):
        job, session, _ = self.bundle.previs_jobs.create('dance')
        with self.bundle.previs_jobs.state.transaction() as state:
            state['jobs'][job['id']]['lease'] = 0
        restored = file_repositories(self.root)
        self.assertEqual(restored.previs_jobs.prompt_context(session), '')
        self.assertEqual(restored.previs_jobs.create('new', session)[0]['version'], 2)

    def test_processes_serialize_json_updates(self):
        from local_support.state import JsonState
        path = self.root / 'counter.json'
        script = (
            'from pathlib import Path\n'
            'from local_support.state import JsonState\n'
            'import sys\n'
            'state = JsonState(Path(sys.argv[1]), {"count": 0})\n'
            'for _ in range(8):\n'
            '    with state.transaction() as value:\n'
            '        value["count"] += 1\n'
        )
        environment = {**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path[:2])}
        children = [subprocess.Popen([sys.executable, '-B', '-c', script, str(path)],
                                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for _ in range(3)]
        for child in children:
            _, errors = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, errors.decode(errors='replace'))
        self.assertEqual(JsonState(path, {'count': 0}).value['count'], 24)

    def test_json_return_values_do_not_alias_saved_state(self):
        identifier = str(uuid4())
        self.bundle.video_jobs.create(identifier, {'jobId': 'source'}, {})
        row = self.bundle.video_jobs.get(identifier)
        row['metadata']['changed'] = True
        self.assertNotIn('changed', self.bundle.video_jobs.get(identifier)['metadata'])

    def test_result_publication_requires_owner(self):
        store = self.bundle.video_jobs
        identifier = str(uuid4())
        store.create(identifier, {'jobId': 'source'}, {})
        store.claim(identifier, 'owner')
        store.update(identifier, owner='owner', state='downloading')
        candidate = self.root / 'candidate.mp4'
        target = self.root / 'result.mp4'
        candidate.write_bytes(b'video')
        self.assertFalse(store.publish_result(identifier, 'other', candidate, target))
        self.assertTrue(candidate.exists())
        self.assertTrue(store.publish_result(identifier, 'owner', candidate, target))
        self.assertEqual(store.get(identifier)['state'], 'completed')
        self.assertEqual(target.read_bytes(), b'video')

    def test_factory_uses_json_with_database_connections_blocked(self):
        import os
        from ai_video.video_higgsfield_jutsu.python_backend.repositories import local_repositories
        with patch.dict(os.environ, {'PREVIS_STORAGE_MODE': 'local-files'}):
            bundle = local_repositories(self.root)
            self.assertEqual(type(bundle.video_jobs).__name__, 'FileVideoJobs')
            from service_app import create_app
            from fastapi.testclient import TestClient
            app = create_app(previs_root=self.root, repositories=bundle, python_client=SimpleNamespace(status={}))
            client = TestClient(app)
            for path in ('/api/video/jobs', '/api/usage-history?limit=20', '/api/recovered'):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIsInstance(response.json(), dict)

    def test_normal_app_startup_and_oauth_use_no_database(self):
        import os
        from service_app import create_app
        from ai_video.jutsu_auth import create_client
        with patch.dict(os.environ, {'PREVIS_STORAGE_MODE': 'local-files',
                                    'JUTSU_AUTH_DATA_DIR': str(self.root / 'auth')}):
            app = create_app(previs_root=self.root)
            self.assertIsNotNone(app)
            client = create_client('http://127.0.0.1:9000/api/jutsu/callback', data_directory=self.root / 'auth')
            client.storage.save('client', {'client_id': 'local'})
            client.storage.begin('state')
            client.storage.answer('code', 'state')
            self.assertEqual(client.storage.take('state'), ('code', 'state'))
            self.assertIsNone(client.storage.take('state'))
            with self.assertRaises(ValueError):
                client.storage.answer('replay', 'state')
            restored = create_client('http://127.0.0.1:9000/api/jutsu/callback', data_directory=self.root / 'auth')
            self.assertEqual(restored.storage.load('client')['client_id'], 'local')

    def test_character_voice_is_preserved(self):
        record = {'character_id': 'member', 'identity_key': 'asset',
                  'voice_language': 'ko', 'voice_description': ''}
        store = self.bundle.video_jobs
        store.register_characters([record])
        store.save_character_voices([{**record, 'voice_description': 'deep'}])
        restored = file_repositories(self.root).video_jobs
        self.assertEqual(restored.register_characters([record])[0]['voice_description'], 'deep')

    def test_local_code_and_state_are_excluded_from_deployment(self):
        root = Path(__import__('service_app').__file__).parent
        if (STAGE / '.dockerignore').exists():
            root = STAGE
        self.assertIn('/local_support/', (root / '.dockerignore').read_text())
        self.assertIn('/local_support/', (root / '.gitignore').read_text())
        self.assertIn('**/runtime/', (root / '.gitignore').read_text())


class LegacyMigrationTests(unittest.TestCase):
    def test_offline_migration_keeps_jobs_voices_history_and_retention(self):
        from ai_video.video_higgsfield_jutsu.python_backend.job_repository import VideoJobs
        from ai_video.video_higgsfield_jutsu.python_backend.usage_history import UsageHistory
        from ai_video.video_higgsfield_jutsu.python_backend.media_archive import MediaArchive
        from local_support.migrate import migrate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_id = str(uuid4())
            video_id = str(uuid4())
            jobs = VideoJobs(root / 'runtime/data/wan-jobs.sqlite3')
            jobs.create(video_id, {'jobId': source_id}, {})
            jobs.update(video_id, provider_id='provider-id', state='completed')
            history = UsageHistory(root / 'runtime/archive/usage-history.sqlite3')
            history.save_previs({'id': source_id, 'request': 'original'}, 'completed')
            history.set_hidden(source_id, True)
            archive = MediaArchive(root)
            media = root / 'runtime/archive/example.mp4'
            media.write_bytes(b'old')
            archive.track('old', media, created=100)
            self.assertEqual(migrate(root), {'videos': 1, 'history': 1, 'media': 1})
            self.assertEqual(migrate(root), {'videos': 0, 'history': 0, 'media': 0})
            with patch('sqlite3.connect', side_effect=AssertionError('SQLite called after migration')):
                restored = file_repositories(root)
                self.assertEqual(restored.video_jobs.get(video_id)['provider_id'], 'provider-id')
                self.assertEqual(restored.history.get(source_id)['request'], 'original')
                self.assertIn(source_id, restored.history.state.value['hidden'])
                self.assertEqual(restored.archive.record('old')['savedAt'], 100)


if __name__ == '__main__':
    unittest.main()
