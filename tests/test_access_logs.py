import logging
import unittest
from service_app import ChatPollingAccessFilter


class AccessLogTests(unittest.TestCase):
    def setUp(self):
        self.access_filter = ChatPollingAccessFilter()

    def record(self, method, path, status):
        return logging.LogRecord('uvicorn.access', logging.INFO, '', 0,
                                 '%s - "%s %s HTTP/%s" %d',
                                 ('client', method, path, '1.1', status), None)

    def test_successful_delete_access_log_is_silent(self):
        record = self.record('DELETE', '/api/usage-history/job-123', 200)
        self.assertFalse(self.access_filter.filter(record))

    def test_successful_alias_delete_access_log_is_silent(self):
        record = self.record('DELETE', '/AI_video/jutsu/api/usage-history/job-123?x=1', 200)
        self.assertFalse(self.access_filter.filter(record))

    def test_video_partial_requests_are_silent(self):
        for prefix in ('', '/AI_video/jutsu'):
            for path in ('/api/jobs/job-123/media/0', '/api/video/jobs/video-123/video'):
                with self.subTest(path=prefix + path):
                    self.assertFalse(self.access_filter.filter(self.record('GET', prefix + path, 206)))
        self.assertFalse(self.access_filter.filter(self.record('GET', '/AI_video/jobs/job-123/media/0', 206)))

    def test_successful_scenario_snapshot_reads_are_silent(self):
        for prefix in ('', '/AI_video/jutsu'):
            for status in (200, 206):
                path = prefix + '/api/video/jobs/8aad66d1-ce6a-4b04-98f8-892e82f98732/scenario-snapshots/0?x=1'
                with self.subTest(path=path, status=status):
                    self.assertFalse(self.access_filter.filter(self.record('GET', path, status)))

    def test_snapshot_failures_and_unrelated_requests_remain_visible(self):
        path = '/api/video/jobs/video-123/scenario-snapshots/0'
        for method, target, status in (
            ('GET', path, 404), ('GET', path, 500), ('POST', path, 200),
            ('GET', path + '/other', 200), ('GET', '/other/scenario-snapshots/0', 200),
        ):
            with self.subTest(method=method, target=target, status=status):
                self.assertTrue(self.access_filter.filter(self.record(method, target, status)))

    def test_errors_and_other_requests_are_preserved(self):
        for method, path, status in (
            ('DELETE', '/api/usage-history/job-123', 404),
            ('DELETE', '/api/usage-history/job-123', 500),
            ('GET', '/api/jobs/job-123/media/0', 500),
            ('GET', '/api/video/jobs/video-123/guides/0', 404),
            ('GET', '/api/usage-history', 500),
            ('GET', '/api/usage-history/job-123', 200),
            ('GET', '/other', 206),
            ('GET', '/health', 200),
        ):
            record = self.record(method, path, status)
            before = record.getMessage()
            self.assertTrue(self.access_filter.filter(record))
            self.assertEqual(record.getMessage(), before)

    def test_polling_remains_silent(self):
        self.assertFalse(self.access_filter.filter(self.record('GET', '/api/jobs/58793115-d4af-4f03-b9ca-890011f7b641', 200)))
        self.assertFalse(self.access_filter.filter(self.record('GET', '/api/jobs/58793115-d4af-4f03-b9ca-890011f7b641?completeOnly=true', 204)))

    def test_successful_console_reads_are_silent(self):
        for prefix in ('', '/AI_video/jutsu'):
            for path in (
                '/api/video/jobs/4d76af52-d115-4610-9e0d-154b332c353f',
                '/api/usage-history?offset=0&limit=20&hidden=false',
                '/api/video/jobs/video-123/guides/0',
                '/api/jobs/job-123/media/0',
                '/api/video/jobs/video-123/video',
            ):
                with self.subTest(path=prefix + path):
                    self.assertFalse(self.access_filter.filter(self.record('GET', prefix + path, 200)))

    def test_unstructured_record_is_preserved(self):
        record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, 'plain', (), None)
        self.assertTrue(self.access_filter.filter(record))


if __name__ == '__main__':
    unittest.main()
