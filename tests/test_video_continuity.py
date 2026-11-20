import unittest
from unittest.mock import AsyncMock, patch
from ai_video.video_higgsfield_jutsu.python_backend.video_common import build_video_prompt, build_model_video_prompt

class ContinuityTests(unittest.TestCase):
    def test_normal_prompt_includes_continuity(self):
        self.assertIn('SPATIAL AND TEMPORAL CONTINUITY', build_video_prompt('Walk outside.'))

    def test_image_adapter_includes_continuity(self):
        self.assertIn('SPATIAL AND TEMPORAL CONTINUITY', build_model_video_prompt({'image_path':'start.png'}, 'Walk.', max_length=20000))

class ReviewContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_only_repair_keeps_continuity(self):
        from ai_video.video_higgsfield_jutsu.python_backend.video_validation import validate_video_request
        replies=[{'issues':['Location jump'], 'assetMappings':[], 'resolvedPrompt':'Walk through the same door.', 'summary':'Repair'},
                 {'issues':[], 'assetMappings':[], 'resolvedPrompt':'Walk through the same door.', 'summary':'OK'}]
        with patch('ai_video.video_higgsfield_jutsu.python_backend.video_validation.analyze', new=AsyncMock(side_effect=replies)) as analyze:
            prompt, report = await validate_video_request(AsyncMock(), model='test', prompt='Walk.',
                user_prompt='Walk.', scene_prompt='', images=[], reference=None, directory='unused', prompt_limit=20000)
        self.assertIn('SPATIAL AND TEMPORAL CONTINUITY', prompt)
        self.assertNotIn("Video 1's", prompt)
        self.assertIn('continuityRules', analyze.call_args.args[5])

if __name__ == '__main__':
    unittest.main()
