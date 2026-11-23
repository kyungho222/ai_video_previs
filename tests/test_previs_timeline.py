import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ai_video.video_higgsfield_jutsu.python_backend.previs_timeline import (
    endpoint_guides, timeline_prompt, validate_timeline, preserve_timeline,
)


def plan():
    return {
        'summary': 'Two characters approach each other.',
        'entities': ['A is the white-uniform player.', 'B is the blue tiger mascot.'],
        'uncertainties': ['Absolute camera coordinates cannot be measured from MP4.'],
        'segments': [
            {'start': 0, 'end': 4, 'movement': 'Dolly forward, camera stays south of subjects.',
             'speed': 'Smooth acceleration.', 'framing': 'Wide two-shot, A left and B right.',
             'target': 'Midpoint of A and B.', 'endState': 'Medium two-shot.',
             'staging': 'A travels right, B travels left.', 'events': 'Approach.',
             'transition': 'Continuous into next segment.'},
            {'start': 4, 'end': 10, 'movement': 'Crane vertically upward without orbit.',
             'speed': 'Accelerate then decelerate.', 'framing': 'Top-down wide.',
             'target': 'Midpoint of A and B.', 'endState': 'Both visible in top view.',
             'staging': 'A left, B right, both stop near center.', 'events': 'Meet.',
             'transition': 'Finish at rest.'},
        ],
    }


class TimelineTests(unittest.TestCase):
    def test_api_default_and_retry_keep_text_endpoint_options(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ai_video.video_higgsfield_jutsu.python_backend.media_archive import MediaArchive
        from ai_video.video_higgsfield_jutsu.python_backend.video_generation import create_video_router
        from ai_video.video_higgsfield_jutsu.python_backend.video_models import VideoCapabilities
        with TemporaryDirectory() as folder:
            root = Path(folder)
            service = SimpleNamespace(root=root, archive=MediaArchive(root), transport=None,
                                      config={'OPENAI_API_KEY': 'mock'}, max_concurrent_jobs=2)
            router = create_video_router(service)
            manager = router.manager
            manager.adapters['wan'] = SimpleNamespace(
                configured=True,
                capabilities=VideoCapabilities(provider='wan', label='Wan', model='test', regions=('singapore',)),
                build_media=Mock(return_value=[]),
            )
            manager.source = Mock(return_value={})
            manager.scene_prompt = Mock(return_value='Meet.')
            manager.assets.resolve = Mock(return_value=[{'id': 'asset', 'role': 'character'}])
            manager.ensure = Mock()
            app = FastAPI()
            app.include_router(router)
            body = dict(requestId='12345678-1234-1234-1234-123456789abc', source={},
                        prompt='Meet.', duration=10, resolution='480P', region='singapore')
            with patch('ai_video.video_higgsfield_jutsu.python_backend.video_routes.filter_user_prompts',
                       new=AsyncMock(return_value={'status': 'passed'})), TestClient(app) as client:
                response = client.post('/api/video/generate', json=body)
                self.assertEqual(response.status_code, 202, response.text)
                options = response.json()['options']
                self.assertEqual(options['referenceMode'], 'spatial')
                self.assertEqual(options['spatialInput'], 'first_last')
                self.assertEqual(options['spatialGuideCount'], 2)
                self.assertEqual(options['previsTranscript'], 'camera_timeline_v1')
                repeated = client.post('/api/video/generate', json=body)
                self.assertEqual(repeated.status_code, 202, repeated.text)
                self.assertEqual(repeated.json()['options'], options)
                continuous_body = {**body, 'requestId': '42345678-1234-1234-1234-123456789abc',
                                   'source': {'uploadedId': '52345678-1234-1234-1234-123456789abc'},
                                   'referenceMode': 'text_continuity', 'spatialInput': 'first_last'}
                continuous = client.post('/api/video/generate', json=continuous_body)
                self.assertEqual(continuous.status_code, 202, continuous.text)
                continuous_options = continuous.json()['options']
                self.assertEqual(continuous_options['sceneFlowPolicy'], 'first-scene-continuity-v1')
                self.assertEqual(continuous_options['spatialInput'], 'references')
                self.assertEqual(continuous_options['spatialGuideCount'], 3)
                retried_continuous = client.post('/api/video/generate', json={
                    **continuous_body, 'referenceMode': 'auto',
                })
                self.assertEqual(retried_continuous.status_code, 202, retried_continuous.text)
                self.assertEqual(retried_continuous.json()['options'], continuous_options)
                legacy_body = {**body, 'requestId': '22345678-1234-1234-1234-123456789abc',
                               'source': {'uploadedId': '32345678-1234-1234-1234-123456789abc'},
                               'referenceMode': 'video', 'characterMotion': 'reference'}
                legacy = client.post('/api/video/generate', json=legacy_body)
                self.assertEqual(legacy.status_code, 202, legacy.text)
                self.assertNotIn('previsTranscript', legacy.json()['options'])
                self.assertEqual(legacy.json()['options']['referenceMode'], 'video')

    def test_preserves_camera_timing_and_scales_requested_duration(self):
        validated = validate_timeline(plan(), 10)
        text = timeline_prompt(validated, 10, 20)
        self.assertIn('8.000–20.000s', text)
        self.assertIn('Crane vertically upward without orbit.', text)
        self.assertIn('A travels right, B travels left.', text)
        self.assertNotIn('Video 1', text)

    def test_rejects_gaps_overlaps_and_nonfinite_times(self):
        for start in [3, 5, float('nan')]:
            value = copy.deepcopy(plan())
            value['segments'][1]['start'] = start
            with self.assertRaises(ValueError):
                validate_timeline(value, 10)

    def test_keeps_both_generated_endpoints_with_quality_warning(self):
        guides = [{'index': 0, 'name': 'spatial-0.png', 'status': 'verified'},
                  {'index': 1, 'name': 'spatial-1.png', 'status': 'invalid'}]
        retained, warnings = endpoint_guides(guides)
        self.assertEqual(retained, guides)
        self.assertEqual(warnings, [1])

    def test_retains_endpoint_excluded_after_three_quality_corrections(self):
        guides = [{'index': 0, 'name': 'spatial-0.png', 'status': 'verified'},
                  {'index': 1, 'name': 'spatial-1.png', 'status': 'excluded',
                   'generationAttempts': 3, 'sha256': 'generated-image-hash',
                   'verification': {'cameraCompositionPreserved': False,
                                    'issues': ['Restore the high-angle wide view.']}}]
        retained, warnings = endpoint_guides(guides)
        self.assertEqual(retained, guides)
        self.assertEqual(warnings, [1])

    def test_missing_endpoint_cannot_silently_become_first_frame_only(self):
        with self.assertRaises(ValueError):
            endpoint_guides([{'index': 0, 'name': 'spatial-0.png', 'status': 'verified'}])

    def test_validator_rewrite_cannot_discard_camera_transcript(self):
        transcript = timeline_prompt(plan(), 10, 10)
        restored = preserve_timeline('Natural walk.', transcript, 20000)
        self.assertIn(transcript, restored)
        self.assertEqual(preserve_timeline(restored, transcript, 20000), restored)

    def test_provider_payload_contains_two_endpoint_images(self):
        from ai_video.video_higgsfield_jutsu.python_backend.wan import wan_guide_media
        media = wan_guide_media('spatial', 'first_last', [],
                                [{'url': 'start'}, {'url': 'end'}], None)
        self.assertEqual(media, [{'type': 'first_frame', 'url': 'start'},
                                 {'type': 'last_frame', 'url': 'end'}])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_repairs_invalid_timing_and_reuses_valid_cache(self):
        from ai_video.video_higgsfield_jutsu.python_backend.previs_timeline import describe_previs
        bad = copy.deepcopy(plan())
        bad['segments'][1]['start'] = 5
        module = 'ai_video.video_higgsfield_jutsu.python_backend.previs_timeline'
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'previs.mp4'
            source.write_bytes(b'source')
            frames = [{'time': 0, 'path': source}, {'time': 9.96, 'path': source}]
            with patch(module + '.inspect_video', return_value={'duration': 10}), \
                 patch(module + '.sample_frames', return_value=frames), \
                 patch(module + '.SpatialGuideBuilder.analyze', new=AsyncMock(side_effect=[bad, plan()])) as analyze:
                args = dict(request=AsyncMock(), model='review', brief='Meet.', assets=[])
                value = await describe_previs(source, Path(folder) / 'samples', **args)
                self.assertEqual(analyze.await_count, 2)
                self.assertIn('contiguous', analyze.call_args.args[2])
                self.assertEqual(await describe_previs(source, Path(folder) / 'samples',
                                                      cached=value, **args), value)
                self.assertEqual(analyze.await_count, 2)

    async def test_normal_submission_uploads_only_two_images_and_preserves_camera_after_review(self):
        from ai_video.video_higgsfield_jutsu.python_backend.video_generation import VideoManager
        from ai_video.video_higgsfield_jutsu.python_backend.video_models import VideoCapabilities
        module = 'ai_video.video_higgsfield_jutsu.python_backend.video_preparation'
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'source.mp4'
            original.write_bytes(b'source')
            guides = root / 'guides'
            guides.mkdir()
            for index in range(2):
                (guides / f'spatial-{index}.png').write_bytes(b'image')
            options = dict(referenceMode='spatial', spatialInput='first_last',
                           previsTranscript='camera_timeline_v1', prompt='Meet.', duration=10,
                           resolution='480P', region='singapore', characterMotion='independent')
            job = dict(id='test', state='queued', options=options, source={}, metadata={}, created=1)
            adapter = SimpleNamespace(
                capabilities=VideoCapabilities(provider='fake', label='Fake', model='fake', regions=('singapore',)),
                upload=AsyncMock(side_effect=['start-url', 'end-url']), submit=AsyncMock(return_value='task'),
                build_media=lambda *args: [{'type': 'first_frame'}, {'type': 'last_frame'}],
            )
            manager = VideoManager.__new__(VideoManager)
            manager.service = SimpleNamespace(config={'OPENAI_API_KEY': 'mock'},
                                              archive=SimpleNamespace(track=Mock()), default_model='review')
            manager.root = root
            manager.store = SimpleNamespace(begin_submission=Mock(return_value=True), get=lambda _: job)
            manager.source = lambda _: {'path': original}
            manager.adapter_for = lambda _: adapter
            manager.native_scene_manifest = AsyncMock(return_value=None)
            manager._analysis_request = AsyncMock()
            manager.assets = SimpleNamespace()
            bundle = dict(guides=[dict(index=i, name=f'spatial-{i}.png', time=i * 9.96)
                                  for i in range(2)])
            manager._prepare_spatial_guides = AsyncMock(return_value=(bundle, 'spatial', 'first_last'))
            transcript = dict(plan=plan(), sourceDuration=10)
            with patch(module + '.inspect_video', return_value={'duration': 10}), \
                 patch(module + '.describe_previs', new=AsyncMock(return_value=transcript)), \
                 patch(module + '.spatial_prompt_context', return_value={}), \
                 patch(module + '.scene_text_without_asset_indices', side_effect=lambda text, _: text), \
                 patch(module + '.optimize_video_prompt', new=AsyncMock(return_value={'prompt': 'Scene.', 'optimization': {}})), \
                 patch(module + '.bind_spatial_prompt', side_effect=lambda text, *args: text), \
                 patch(module + '.validate_video_request', new=AsyncMock(return_value=(
                     'AI rewritten scene.', dict(status='passed', attempts=[], issues=[])))):
                await manager._prepare_submission(job, 'owner', root / 'reference.mp4', root,
                                                  lambda **values: job.update(values))
            self.assertEqual(adapter.upload.await_count, 2)
            self.assertTrue(all(call.kwargs['media_type'] == 'image/png' for call in adapter.upload.call_args_list))
            args, kwargs = adapter.submit.call_args
            self.assertIsNone(args[0])
            self.assertEqual(kwargs['spatial_input'], 'first_last')
            self.assertEqual(kwargs['reference_mode'], 'spatial')
            self.assertIn('Crane vertically upward without orbit.', kwargs['final_prompt'])
            self.assertFalse(job['metadata']['referenceVideoSent'])
