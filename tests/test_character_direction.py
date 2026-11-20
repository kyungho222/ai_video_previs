import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace
from ai_video.video_higgsfield_jutsu.python_backend.video_common import VideoJobs

ASSETS=[{'id':'sheet-asset','role':'character','name':'Group'}, {'id':'stage','role':'background','name':'Stage'}]
def bundle():
    return {'assetParts':[{'sourceIndex':1,'label':'A','bbox':[0,0,.5,1]},
                          {'sourceIndex':1,'label':'B','bbox':[.5,0,1,1]}, {'sourceIndex':2}],
            'mapping':{'entities':[{'id':'left','assetIndices':[1],'label':'A','appearance':'Pink jacket','sourceObjectIds':['rootA'],'frames':[]},
                                   {'id':'right','assetIndices':[2],'label':'B','appearance':'Blue jacket','sourceObjectIds':['rootB'],'frames':[]},
                                   {'id':'stage','assetIndices':[3],'label':'Stage','appearance':'LED stage','sourceObjectIds':[],'frames':[]}]}}

def direction(cast):
    a,b=[r['character_id'] for r in cast]
    return {'characters':[{'character_id':a,'voice_language':'ko','voice_description':'Gentle low male voice'},
                          {'character_id':b,'voice_language':'ko','voice_description':'Bright mid-range male voice'}],
            'actions':[{'action_id':'action1','character_id':a,'start':0,'end':2,'description':'Wave', 'target_ids':[b]}],
            'dialogues':[{'dialogue_id':'line1','speaker_ids':[a,b],'start':0,'end':2,'text':'안녕하세요!','delivery':'Shared greeting'},
                         {'dialogue_id':'line2','speaker_ids':[a],'start':2,'end':4,'text':'반가워요!','delivery':'Warm reply'}]}

class CharacterRegistryTests(unittest.TestCase):
    def test_ids_persist_and_different_panels_are_distinct(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records
        records=character_records(ASSETS,bundle())
        self.assertEqual(len(records),2)
        self.assertNotEqual(records[0]['character_id'],records[1]['character_id'])
        revised=bundle();revised['assetParts'][0]['bbox'][2]=.49
        self.assertEqual([r['character_id'] for r in records],
                         [r['character_id'] for r in character_records(ASSETS,revised)])
        with TemporaryDirectory() as folder:
            path=Path(folder)/'jobs.sqlite3';store=VideoJobs(path)
            cast=store.register_characters(records)
            store.save_character_voices(direction(cast)['characters'])
            restored=VideoJobs(path).register_characters(records)
            self.assertEqual(restored[0]['voice_description'],'Gentle low male voice')

    def test_unknown_speaker_or_target_is_rejected(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records,validate_direction
        cast=character_records(ASSETS,bundle());value=direction(cast)
        value['dialogues'][0]['speaker_ids']=['unknown']
        with self.assertRaises(ValueError):validate_direction(value,cast,10,{'stage'})
        value=direction(cast);value['actions'][0]['target_ids']=['unknown']
        with self.assertRaises(ValueError):validate_direction(value,cast,10,{'stage'})

    def test_voice_conflict_rolls_back_the_whole_write(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records
        with TemporaryDirectory() as folder:
            store = VideoJobs(Path(folder) / 'jobs.sqlite3')
            records = character_records(ASSETS, bundle())
            cast = store.register_characters(records)
            profiles = direction(cast)['characters']
            store.save_character_voices([profiles[1]])
            profiles[1]['voice_description'] = 'Changed voice'
            with self.assertRaises(ValueError):
                store.save_character_voices(profiles)
            restored = store.register_characters(records)
            self.assertEqual(restored[0]['voice_description'], '')
            self.assertEqual(restored[1]['voice_description'], 'Bright mid-range male voice')

    def test_joint_greeting_and_independent_action_pass(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records,validate_direction,direction_prompt
        cast=character_records(ASSETS,bundle());value=direction(cast)
        validate_direction(value,cast,10,{'stage'})
        text=direction_prompt(cast,value)
        self.assertIn(cast[0]['character_id'],text)
        self.assertIn(cast[1]['character_id'],text)
        self.assertIn('안녕하세요!',text)

class DirectionRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_planner_can_resume_when_api_becomes_available(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records, prepare_character_direction
        with TemporaryDirectory() as folder:
            store = VideoJobs(Path(folder) / 'jobs.sqlite3')
            cast = store.register_characters(character_records(ASSETS, bundle()))
            args = dict(store=store, cast=cast, model='test', user_prompt='Greet.',
                        scene_prompt='', duration=10, source_identity={}, context={})
            unavailable = await prepare_character_direction(None, **args)
            self.assertEqual(unavailable['status'], 'unavailable')
            request = AsyncMock(return_value={
                'output': [{'content': [{'type': 'output_text', 'text': json.dumps(direction(cast))}]}],
            })
            ready = await prepare_character_direction(request, cached=unavailable, **args)
            self.assertEqual(ready['status'], 'ready')
            self.assertEqual(request.await_count, 1)

    async def test_submission_preserves_cast_after_prompt_rewrite(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records
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
                           prompt='Both greet, then A waves to B.', duration=10,
                           assets=ASSETS, resolution='480P', region='singapore',
                           characterMotion='independent')
            job = dict(id='test', state='queued', options=options, source={}, metadata={}, created=1)
            from ai_video.video_higgsfield_jutsu.python_backend.prompt_filter import presentation_rules
            rules = presentation_rules({'sexual':1,'violence':0})
            job['metadata']['promptFilter'] = {'generationRules':rules}
            adapter = SimpleNamespace(
                capabilities=VideoCapabilities(provider='fake', label='Fake', model='fake', regions=('singapore',)),
                upload=AsyncMock(side_effect=['start-url', 'end-url']),
                submit=AsyncMock(return_value='task'),
                build_media=lambda *args: [{'type': 'first_frame'}, {'type': 'last_frame'}],
            )
            registry = VideoJobs(root / 'jobs.sqlite3')
            manager = VideoManager.__new__(VideoManager)
            manager.service = SimpleNamespace(config={'OPENAI_API_KEY': 'mock'},
                                              archive=SimpleNamespace(track=Mock()), default_model='review')
            manager.root = root
            manager.store = SimpleNamespace(
                begin_submission=Mock(return_value=True), get=lambda _: job,
                heartbeat=Mock(return_value=True), register_characters=registry.register_characters,
                save_character_voices=registry.save_character_voices,
            )
            manager.source = lambda _: {'path': original}
            manager.adapter_for = lambda _: adapter
            manager.assets = SimpleNamespace(get=lambda identifier: (
                next(asset for asset in ASSETS if asset['id'] == identifier), original))
            guide_bundle = bundle()
            guide_bundle['guides'] = [dict(index=i, name=f'spatial-{i}.png', time=i * 9.96)
                                      for i in range(2)]
            manager._prepare_spatial_guides = AsyncMock(return_value=(guide_bundle, 'spatial', 'first_last'))
            cast = character_records(ASSETS, guide_bundle)
            manager._analysis_request = AsyncMock(return_value={
                'output': [{'content': [{'type': 'output_text', 'text': json.dumps(direction(cast))}]}],
            })
            with patch(module + '.inspect_video', return_value={'duration': 10}), \
                 patch(module + '.spatial_prompt_context', return_value={'entities': guide_bundle['mapping']['entities']}), \
                 patch(module + '.scene_text_without_asset_indices', side_effect=lambda text, _: text), \
                 patch(module + '.optimize_video_prompt', new=AsyncMock(return_value={'prompt': 'Scene.', 'optimization': {}})), \
                 patch(module + '.bind_spatial_prompt', side_effect=lambda text, *args: text), \
                 patch(module + '.validate_video_request', new=AsyncMock(return_value=(
                     'AI rewritten scene.', dict(status='passed', attempts=[], issues=[])))):
                await manager._prepare_submission(job, 'owner', root / 'reference.mp4', root,
                                                  lambda **values: job.update(values))
            prompt = adapter.submit.call_args.kwargs['final_prompt']
            for character in cast:
                self.assertIn(character['character_id'], prompt)
                self.assertIn(character['voice_profile_id'], prompt)
            self.assertIn('안녕하세요!', prompt)
            self.assertIn('target_ids', prompt)
            self.assertIn(rules, prompt)
            self.assertIn(rules, manager._analysis_request.call_args.args[1]['instructions'])
            self.assertEqual(job['metadata']['characterDirection']['status'], 'ready')
            self.assertEqual(adapter.upload.await_count, 2)
            self.assertIsNone(adapter.submit.call_args.args[0])

    async def test_three_attempts_then_warning_fallback(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records,prepare_character_direction
        with TemporaryDirectory() as folder:
            store=VideoJobs(Path(folder)/'jobs.sqlite3')
            cast=store.register_characters(character_records(ASSETS,bundle()))
            request=AsyncMock(return_value={'output':[{'content':[{'type':'output_text','text':'{}'}]}]})
            snapshots=[]
            result=await prepare_character_direction(request,store=store,cast=cast,model='test',
                user_prompt='Greet.',scene_prompt='',duration=10,source_identity={},context={},
                save=lambda record:snapshots.append(copy.deepcopy(record)))
            self.assertEqual(request.await_count,3)
            self.assertEqual(result['status'],'needs_review')
            self.assertIsNone(result['plan'])
            again=await prepare_character_direction(request,store=store,cast=cast,model='test',
                user_prompt='Greet.',scene_prompt='',duration=10,source_identity={},context={},cached=result)
            self.assertEqual(request.await_count,3)
            self.assertEqual(again,result)

    async def test_valid_plan_is_saved_and_reused(self):
        from ai_video.video_higgsfield_jutsu.python_backend.character_direction import character_records,prepare_character_direction
        with TemporaryDirectory() as folder:
            store=VideoJobs(Path(folder)/'jobs.sqlite3')
            cast=store.register_characters(character_records(ASSETS,bundle()));plan=direction(cast)
            request=AsyncMock(return_value={'output':[{'content':[{'type':'output_text','text':json.dumps(plan)}]}]})
            args=dict(store=store,cast=cast,model='test',user_prompt='Greet.',scene_prompt='',duration=10,
                      source_identity={},context={})
            result=await prepare_character_direction(request,**args)
            self.assertEqual(result['status'],'ready')
            self.assertEqual((await prepare_character_direction(request,cached=result,**args)),result)
            self.assertEqual(request.await_count,1)

if __name__=='__main__':unittest.main()
