import unittest
from unittest.mock import AsyncMock,patch
from ai_video.video_higgsfield_jutsu.python_backend.video_common import build_video_prompt,build_model_video_prompt,PromptLengthError
from ai_video.video_higgsfield_jutsu.python_backend.video_validation import validate_video_request

class SpeechLanguageTests(unittest.TestCase):
    def test_shared_prompts_include_fixed_language_policy(self):
        from ai_video.video_higgsfield_jutsu.python_backend.video_common import SPEECH_LANGUAGE_RULES
        self.assertIn(SPEECH_LANGUAGE_RULES,build_video_prompt('A character responds.'))
        self.assertIn(SPEECH_LANGUAGE_RULES,build_model_video_prompt({'image_path':'image.png'},'A reply.',max_length=20000))
        self.assertIn('cartoon',SPEECH_LANGUAGE_RULES)
        self.assertIn('East Asian',SPEECH_LANGUAGE_RULES)
        self.assertIn('English',SPEECH_LANGUAGE_RULES)
        self.assertIn('explicit',SPEECH_LANGUAGE_RULES)

class SpeechValidationTests(unittest.IsolatedAsyncioTestCase):
    async def validate(self,request,prompt='A reply.',limit=20000):
        return await validate_video_request(request,model='test',prompt=prompt,user_prompt='A reply.',
            scene_prompt='',images=[],reference=None,directory='unused',prompt_limit=limit)

    async def test_policy_applies_without_ai_review(self):
        text,_=await self.validate(None)
        self.assertIn('SPOKEN DIALOGUE LANGUAGE',text)

    async def test_policy_survives_prompt_repair(self):
        from ai_video.video_higgsfield_jutsu.python_backend.video_common import SPEECH_LANGUAGE_RULES
        replies=[{'issues':['Dialogue language mismatch'],'assetMappings':[],'resolvedPrompt':'A Korean reply.','summary':'Repair'},
                 {'issues':[],'assetMappings':[],'resolvedPrompt':'A Korean reply.','summary':'OK'}]
        with patch('ai_video.video_higgsfield_jutsu.python_backend.video_validation.analyze',new=AsyncMock(side_effect=replies)) as analyze:
            text,_=await self.validate(AsyncMock())
        self.assertEqual(text.count(SPEECH_LANGUAGE_RULES),1)
        self.assertIn('speechLanguageRules',analyze.call_args.args[5])

    async def test_policy_counts_toward_provider_prompt_limit(self):
        with self.assertRaises(PromptLengthError):await self.validate(None,limit=5)

if __name__=='__main__':unittest.main()
