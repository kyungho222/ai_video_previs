import unittest
from unittest.mock import AsyncMock
from ai_video.video_higgsfield_jutsu.python_backend.video_common import build_video_prompt,optimize_video_prompt

class SpeakerVoiceTests(unittest.IsolatedAsyncioTestCase):
    def test_multi_speaker_rule_is_in_final_prompt(self):
        prompt=build_video_prompt('Three members each say their own line.')
        self.assertIn('MULTI-SPEAKER VOICE CAST',prompt)
        self.assertIn('persistent speaker label',prompt)
        self.assertIn('distinct voice',prompt)
        self.assertIn('unison',prompt)

    async def test_optimizer_receives_speaker_assignment_rules(self):
        request=AsyncMock(return_value={'output':[{'content':[{'type':'output_text','text':'{"scene":"A, B and C each greet the audience."}'}]}]})
        result=await optimize_video_prompt('A, B and C each greet the audience.',request=request,model='test',duration=10)
        self.assertIn('MULTI-SPEAKER VOICE CAST',request.call_args.args[1]['instructions'])
        self.assertIn('MULTI-SPEAKER VOICE CAST',result['prompt'])

if __name__=='__main__':unittest.main()
