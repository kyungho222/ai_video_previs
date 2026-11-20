import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ai_video.video_higgsfield_jutsu.python_backend import previs_timeline
from ai_video.video_higgsfield_jutsu.python_backend.video_common import build_reference_prompt, optimize_video_prompt, PromptLengthError
from test_previs_timeline import plan


def response(value):
    return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps(value)}]}]}


class PromptBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_camera_and_three_speakers_reach_provider_within_limit(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records, direction_prompt
        from ai_video.video_higgsfield_jutsu.python_backend.video_common import VideoJobs
        from ai_video.video_higgsfield_jutsu.python_backend.video_generation import VideoManager
        from ai_video.video_higgsfield_jutsu.python_backend.video_models import VideoCapabilities
        assets = [dict(id=name, role='character', name=name) for name in ('A', 'B', 'C')]
        mapping = dict(cameraSummary='Track across A, B, C.', entities=[
            dict(id=name, label=name, appearance=name + ' stage costume', assetIndices=[index],
                 sourceObjectIds=[], frames=[])
            for index, name in enumerate(('A', 'B', 'C'), 1)
        ])
        guide_bundle = dict(mapping=mapping, assetParts=[dict(sourceIndex=i) for i in range(1, 4)],
                            guides=[dict(index=i, name=f'spatial-{i}.png', time=i * 9.96) for i in range(2)])
        concise = plan()
        concise['summary'] = 'Three performers greet.'
        concise['entities'] = ['A left.', 'B center.', 'C right.']
        source_plan = copy.deepcopy(concise)
        source_plan['summary'] += ' The same stage and identities remain throughout.' * 280
        self.assertGreater(len(previs_timeline.timeline_prompt(source_plan, 10, 10)), 13769)
        cast = character_records(assets, guide_bundle)
        direction = dict(
            characters=[dict(character_id=c['character_id'], voice_language='ko',
                             voice_description='Distinct voice for ' + c['label']) for c in cast],
            actions=[dict(action_id='wave-' + c['label'], character_id=c['character_id'],
                          start=i * 2, end=i * 2 + 2, description='Wave to the audience.', target_ids=[])
                     for i, c in enumerate(cast)],
            dialogues=[dict(dialogue_id='line-' + c['label'], speaker_ids=[c['character_id']],
                            start=i * 2, end=i * 2 + 2, text=c['label'] + ' 안녕하세요!', delivery='Friendly')
                       for i, c in enumerate(cast)],
        )

        async def request(_, body):
            name = body['text']['format']['name']
            return response({'compact_camera_timeline': concise, 'character_direction': direction,
                             'optimized_video_scene': {'scene': 'A, B and C greet in their assigned turns.'},
                             'fitted_video_prompt': {'prompt': 'A, B and C greet. First and last frames preserve appearance.\n'
                                 + previs_timeline.timeline_prompt(concise, 10, 10) + '\n'
                                 + direction_prompt(cast, direction)}}[name])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'source.mp4'
            original.write_bytes(b'source')
            (root / 'guides').mkdir()
            for i in range(2):
                (root / 'guides' / f'spatial-{i}.png').write_bytes(b'image')
            options = dict(referenceMode='spatial', spatialInput='first_last', assets=assets,
                           previsTranscript='camera_timeline_v1', prompt='Three members greet.',
                           scenePrompt='Three members greet.', duration=10, resolution='480P', region='singapore')
            job = dict(id='test', state='queued', options=options, source={}, metadata={}, created=1)
            registry = VideoJobs(root / 'jobs.sqlite3')
            manager = VideoManager.__new__(VideoManager)
            manager.root = root
            manager.service = SimpleNamespace(config={'OPENAI_API_KEY': 'mock'},
                                              archive=SimpleNamespace(track=Mock()), default_model='review')
            manager.store = SimpleNamespace(begin_submission=Mock(return_value=True), get=lambda _: job,
                                            heartbeat=Mock(return_value=True),
                                            register_characters=registry.register_characters,
                                            save_character_voices=registry.save_character_voices)
            manager.source = lambda _: {'path': original}
            adapter = SimpleNamespace(
                capabilities=VideoCapabilities(provider='fake', label='Fake', model='fake', regions=('singapore',)),
                upload=AsyncMock(side_effect=['start', 'end']), submit=AsyncMock(return_value='task'),
                build_media=lambda *args: [{'type': 'first_frame'}, {'type': 'last_frame'}],
            )
            manager.adapter_for = lambda _: adapter
            manager.assets = SimpleNamespace(get=lambda key: (next(a for a in assets if a['id'] == key), original))
            manager.native_scene_manifest = AsyncMock(return_value=None)
            manager._analysis_request = AsyncMock(side_effect=request)
            manager._prepare_spatial_guides = AsyncMock(return_value=(guide_bundle, 'spatial', 'first_last'))
            module = 'ai_video.video_higgsfield_jutsu.python_backend.video_preparation'

            async def validate(_, **kwargs):
                return kwargs['prompt'], dict(status='passed', attempts=[], issues=[])

            with patch(module + '.inspect_video', return_value={'duration': 10}), \
                 patch(module + '.describe_previs', new=AsyncMock(return_value=dict(plan=source_plan, sourceDuration=10))), \
                 patch(module + '.validate_video_request', new=validate):
                await manager._prepare_submission(job, 'owner', root / 'reference.mp4', root,
                                                  lambda **values: job.update(values))
            final = adapter.submit.call_args.kwargs['final_prompt']
            self.assertLessEqual(len(final), 20000)
            self.assertIn("Default to a natural Korean atmosphere", final)
            self.assertEqual(job['metadata']['characterDirection']['status'], 'ready')
            self.assertEqual(job['metadata']['previsTranscript']['plan'], source_plan)
            self.assertGreater(job['metadata']['promptBudget']['sceneContent'], 1500)
            for character in cast:
                self.assertIn(character['character_id'], final)
                self.assertIn(character['label'] + ' 안녕하세요!', final)
            self.assertIn('Crane vertically upward without orbit.', final)
            self.assertEqual(adapter.upload.await_count, 2)
            self.assertFalse(any(call.args[1]['text']['format']['name'] == 'compact_camera_timeline'
                                 for call in manager._analysis_request.call_args_list))

    async def test_optimizer_receives_real_scene_budget_and_repairs_overlong_output(self):
        limit = len(build_reference_prompt('Short scene.', reference_mode='images')) + 150
        request = AsyncMock(side_effect=[response({'scene': 'Verbose ' * 200}),
                                         response({'scene': 'Short scene.'})])
        result = await optimize_video_prompt('Source scene.', reference_mode='images',
                                             request=request, model='test', max_length=limit)
        self.assertEqual(request.await_count, 2)
        context = json.loads(request.call_args_list[0].args[1]['input'][0]['content'])
        self.assertGreater(context['maxSceneCharacters'], 0)
        self.assertLess(context['maxSceneCharacters'], limit)
        self.assertEqual(result['optimization']['status'], 'optimized')
        self.assertIn('Short scene.', result['prompt'])
        self.assertLessEqual(len(result['prompt']), limit)

    async def test_camera_compression_preserves_timing_and_reuses_saved_result(self):
        original = plan()
        original['summary'] += ' Repeated detail.' * 700
        concise = plan()
        request = AsyncMock(return_value=response(concise))
        snapshots = []
        result = await previs_timeline.compact_timeline(
            original, 10, 10, request=request, model='test', max_chars=3500,
            save=lambda value: snapshots.append(copy.deepcopy(value)),
        )
        self.assertLessEqual(len(result['text']), 3500)
        self.assertEqual(len(result['plan']['segments']), 2)
        self.assertGreater(len(original['summary']), 10000)
        await previs_timeline.compact_timeline(
            original, 10, 10, request=request, model='test', max_chars=3500, cached=result,
        )
        self.assertEqual(request.await_count, 1)
        self.assertTrue(snapshots)

    async def test_camera_compression_repairs_changed_event_time(self):
        original = plan()
        original['summary'] += ' Repeat.' * 1000
        original['criticalMoments'] = [{'time': 4, 'event': 'Meet.', 'requiredView': 'Two-shot.'}]
        good = plan()
        good['criticalMoments'] = copy.deepcopy(original['criticalMoments'])
        bad = copy.deepcopy(good)
        bad['criticalMoments'][0]['time'] = 6
        request = AsyncMock(side_effect=[response(bad), response(good)])
        result = await previs_timeline.compact_timeline(
            original, 10, 10, request=request, model='test', max_chars=3500,
        )
        self.assertEqual(request.await_count, 2)
        self.assertEqual(result['plan']['criticalMoments'][0]['time'], 4)

    async def test_camera_repair_budget_survives_resume(self):
        original = plan()
        original['summary'] += ' Repeat.' * 1000
        request = AsyncMock(return_value=response(original))
        saved = []
        args = dict(request=request, model='test', max_chars=3500,
                    save=lambda value: saved.append(copy.deepcopy(value)))
        with self.assertRaises(PromptLengthError):
            await previs_timeline.compact_timeline(original, 10, 10, **args)
        self.assertEqual(request.await_count, 3)
        with self.assertRaises(PromptLengthError):
            await previs_timeline.compact_timeline(original, 10, 10, cached=saved[-1], **args)
        self.assertEqual(request.await_count, 3)

    async def test_optimizer_fallback_remains_complete_when_ai_is_unavailable(self):
        original = 'Preserve every line and camera action.'
        expected = build_reference_prompt(original, reference_mode='images')
        result = await optimize_video_prompt(original, reference_mode='images', max_length=len(expected))
        self.assertEqual(result['prompt'], expected)

    async def test_final_prompt_is_compacted_only_above_twenty_thousand(self):
        from ai_video.video_higgsfield_jutsu.python_backend.video_common import fit_video_prompt
        prompt = 'Camera A to B. ' * 1600
        request = AsyncMock(return_value=response({'prompt': 'Camera A to B. char_a says 안녕하세요.'}))
        result = await fit_video_prompt(prompt, request=request, model='test', max_length=20000,
                                        required_texts=['char_a', '안녕하세요.'])
        self.assertLessEqual(len(result['prompt']), 20000)
        self.assertEqual(request.await_count, 1)
        short_request = AsyncMock()
        result = await fit_video_prompt('Scene.', request=short_request, model='test', max_length=20000)
        self.assertEqual(result['prompt'], 'Scene.')
        short_request.assert_not_awaited()

    async def test_final_compaction_repairs_missing_speaker_and_keeps_voice_rules(self):
        from ai_video.video_higgsfield_jutsu.python_backend.video_common import fit_video_prompt, SPEECH_LANGUAGE_RULES
        prompt = 'char_a: 안녕하세요. ' * 1500 + '\n' + SPEECH_LANGUAGE_RULES
        request = AsyncMock(side_effect=[response({'prompt': '안녕하세요.'}),
                                         response({'prompt': 'char_a: 안녕하세요.'})])
        result = await fit_video_prompt(prompt, request=request, model='test', max_length=20000,
                                        required_texts=['char_a', '안녕하세요.'])
        self.assertEqual(request.await_count, 2)
        self.assertIn(SPEECH_LANGUAGE_RULES, result['prompt'])
        self.assertLessEqual(len(result['prompt']), 20000)


if __name__ == '__main__':
    unittest.main()
