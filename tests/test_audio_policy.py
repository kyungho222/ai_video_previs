import unittest

from ai_video.video_higgsfield_jutsu.python_backend.character_direction import validate_direction
from ai_video.video_higgsfield_jutsu.python_backend.video_common import build_video_prompt


class AudioPolicyTests(unittest.TestCase):
    def test_generated_music_defaults_to_korean_style_with_user_overrides(self):
        prompt = build_video_prompt('Generate background music for the scene.')
        self.assertIn('KOREAN MUSIC STYLE DEFAULT', prompt)
        self.assertIn('melody, rhythm, instrumentation, and arrangement', prompt)
        self.assertIn('explicit user music styles and genres take precedence', prompt)
        self.assertIn('Do not force traditional Korean instruments', prompt)
        self.assertIn('Do not restyle or regenerate uploaded audio', prompt)

    def test_singing_action_requires_a_voice_without_spoken_dialogue(self):
        cast = [{'character_id':'singer', 'voice_language':'none', 'voice_description':''}]
        plan = {'characters':[{'character_id':'singer','voice_language':'none','voice_description':''}],
                'actions':[{'action_id':'song','character_id':'singer','start':0,'end':5,
                            'description':'Sings a reflective song on stage.','target_ids':[]}],
                'dialogues':[]}
        with self.assertRaisesRegex(ValueError, 'vocal'):
            validate_direction(plan,cast,5)

    def test_audio_defaults_to_voices_and_requires_explicit_music_request(self):
        prompt = build_video_prompt('A foreign singer performs in English.')
        self.assertIn('VOCALS AND ACCOMPANIMENT',prompt)
        self.assertIn('Default audio to requested character voices only.', prompt)
        self.assertIn('only when the user explicitly requests that audio', prompt)
        self.assertIn('without a separate music or accompaniment request uses vocals only', prompt)
        self.assertNotIn('Unless the user requests a cappella', prompt)
        self.assertIn('a cappella',prompt)
        self.assertIn('English',prompt)

    def test_vocal_mode_cannot_override_explicit_singing_action(self):
        from ai_video.video_higgsfield_jutsu.python_backend.audio_policy import requires_vocal_profile
        self.assertTrue(requires_vocal_profile({'vocal_mode':'none','description':'Sings a song.'}))
        self.assertTrue(requires_vocal_profile({'description':'Walks across the stage while singing a song.'}))
        self.assertFalse(requires_vocal_profile({'vocal_mode':'none','description':'Listens to a singer and smiles.'}))
        self.assertFalse(requires_vocal_profile({'description':'Watches the singer singing on stage.'}))
