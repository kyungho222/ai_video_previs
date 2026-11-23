import copy
import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock,patch
from PIL import Image

from ai_video.video_higgsfield_jutsu.python_backend.previs_timeline import validate_timeline,timeline_prompt
from ai_video.video_higgsfield_jutsu.python_backend.video_common import VideoError,VideoJobs
from ai_video.video_higgsfield_jutsu.python_backend.spatial_guides import SpatialGuideBuilder,VERIFY_SCHEMA

SOURCE={'jobId':'12345678-1234-1234-1234-123456789abc','mediaIndex':0}
def plan():
    return {'summary':'Walk out through a door.','entities':['A'],'uncertainties':[],
            'segments':[dict(start=0,end=10,**{key:'Continuous walk' for key in
                ['movement','speed','framing','target','endState','staging','events','transition']})]}

class SourceIntegrityTests(unittest.TestCase):
    def test_hash_and_source_are_pinned(self):
        from ai_video.video_higgsfield_jutsu.python_backend.previs_timeline import bind_source_identity
        with TemporaryDirectory() as folder:
            p=Path(folder)/'video.mp4';p.write_bytes(b'first')
            value=bind_source_identity(SOURCE,p)
            self.assertEqual(value['sha256'],hashlib.sha256(b'first').hexdigest())
            self.assertEqual(bind_source_identity(SOURCE,p,value),value)
            p.write_bytes(b'changed')
            with self.assertRaises(VideoError):bind_source_identity(SOURCE,p,value)

    def test_different_source_cannot_reuse_pin(self):
        from ai_video.video_higgsfield_jutsu.python_backend.previs_timeline import bind_source_identity
        value=bind_source_identity(SOURCE,None)
        with self.assertRaises(VideoError):bind_source_identity({'uploadedId':'other'},None,value)

    def test_initial_metadata_is_saved_with_job_and_retry_preserves_it(self):
        with TemporaryDirectory() as folder:
            store=VideoJobs(Path(folder)/'jobs.sqlite3')
            job,created=store.create('id',SOURCE,{'prompt':'Walk'},initial_metadata={'sourceIdentity':{'sha256':'first'}})
            again,_=store.create('id',SOURCE,{'prompt':'Walk'},initial_metadata={'sourceIdentity':{'sha256':'changed'}})
            self.assertEqual(again['metadata'],job['metadata'])
            self.assertTrue(created)

class TimelineStateTests(unittest.TestCase):
    def test_old_timeline_remains_supported(self):
        self.assertEqual(validate_timeline(plan(),10),plan())

    def test_boundary_states_and_critical_moments_are_transmitted(self):
        value=plan();value['segments'][0]['startPhysicalState']='Inside office, holding person.'
        value['segments'][0]['endPhysicalState']='Outside, person is in bin.'
        value['criticalMoments']=[{'time':4,'event':'Door crossing','requiredView':'Doorway and all cast visible'}]
        text=timeline_prompt(validate_timeline(value,10),10,20)
        self.assertIn('Inside office, holding person.',text)
        self.assertIn('Outside, person is in bin.',text)
        self.assertIn('8.000',text)
        self.assertIn('Door crossing',text)

    def test_critical_moment_outside_video_is_rejected(self):
        value=plan();value['criticalMoments']=[{'time':12,'event':'Release','requiredView':'Hands visible'}]
        with self.assertRaises(ValueError):validate_timeline(value,10)

    def test_endpoint_review_checks_location_and_action_state(self):
        self.assertIn('locationContinuityPreserved',VERIFY_SCHEMA['required'])
        self.assertIn('sceneStatePreserved',VERIFY_SCHEMA['required'])

class LegacyAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_old_analysis_cache_is_reused_for_existing_job(self):
        import json
        from ai_video.video_higgsfield_jutsu.python_backend.previs_timeline import describe_previs
        with TemporaryDirectory() as folder:
            source=Path(folder)/'previs.mp4';source.write_bytes(b'previs')
            signature={'version':1,'model':'test','brief':'Walk','native':None,
                       'source':hashlib.sha256(b'previs').hexdigest(),'assets':[]}
            cached={'version':1,'fingerprint':hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest(),
                    'sourceDuration':10,'plan':plan()}
            with patch('ai_video.video_higgsfield_jutsu.python_backend.previs_timeline.inspect_video',return_value={'duration':10}), \
                 patch('ai_video.video_higgsfield_jutsu.python_backend.previs_timeline.sample_frames') as samples:
                value=await describe_previs(source,Path(folder)/'samples',request=AsyncMock(),model='test',
                                            brief='Walk',assets=[],cached=cached)
            self.assertEqual(value,cached);samples.assert_not_called()

class EndpointRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_location_conflict_stops_at_three_without_reusing_rejected_opening(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);image=root/'asset.png'
            Image.new('RGB',(240,240),'white').save(image)
            image_bytes=image.read_bytes();directory=root/'guides'
            frames=[{'index':i,'frame':i*239,'time':i*9.9583,'width':240,'height':240,'path':image} for i in range(2)]
            mapping={'cameraSummary':'Track through same door','entities':[{
                'id':'A','assetIndices':[1],'label':'A','appearance':'A','sourceObjectIds':[],'confidence':.99,
                'frames':[{'index':i,'visible':True,'bbox':[.2,.2,.8,.8]} for i in range(2)]}]}
            reviews=[]
            async def analyze(name,schema,prompt,images):
                if name=='spatial_asset_mapping':return mapping
                reviews.append((prompt,images))
                return {**{key:True for key in VERIFY_SCHEMA['required'] if key!='issues'},
                        'locationContinuityPreserved':False,'sceneStatePreserved':False,
                        'issues':['Opening must remain indoors; preserve the same doorway.']}
            editor=AsyncMock(return_value=image_bytes)
            builder=SpatialGuideBuilder(AsyncMock(),editor,model='test')
            builder.analyze=analyze
            builder.detect_asset_parts=AsyncMock(return_value=[{'sourceIndex':1}])
            with patch('ai_video.video_higgsfield_jutsu.python_backend.spatial_guides.sample_frames',return_value=frames):
                bundle=await builder.build(image,[{'id':'a','role':'character','path':image}],directory,
                                           count=2,brief='Start indoors, walk outside.',input_format='first_last')
            self.assertEqual(editor.await_count,6)
            self.assertEqual([g['generationAttempts'] for g in bundle['guides']],[3,3])
            self.assertTrue(all(g['status']=='excluded' for g in bundle['guides']))
            self.assertIn('sceneBrief',reviews[0][0])
            self.assertNotIn(directory/'spatial-0.png',reviews[-1][1])

class CriticalSamplingTests(unittest.TestCase):
    def test_output_sampling_includes_event_time(self):
        from ai_video.video_higgsfield_jutsu.python_backend.video_validation import result_samples
        from unittest.mock import MagicMock
        import cv2
        capture=MagicMock()
        capture.get.side_effect=lambda key:30 if key==cv2.CAP_PROP_FPS else 300
        with patch('cv2.VideoCapture',return_value=capture), \
             patch('ai_video.video_higgsfield_jutsu.python_backend.video_validation.review_frames',return_value=[]) as frames:
            result_samples(None,'result.mp4','unused',critical_moments=[{'time':4.1,'event':'Release','requiredView':'Hands'}])
        self.assertIn(123,frames.call_args.kwargs['frame_indices'])

if __name__=='__main__':unittest.main()
