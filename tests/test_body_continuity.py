import unittest

from ai_video.video_higgsfield_jutsu.python_backend.video_prompt import (
    MOTION_RULES,
    build_model_video_prompt,
    build_reference_prompt,
    build_video_prompt,
)


class BodyContinuityTests(unittest.TestCase):
    def test_shared_policy_preserves_connected_body_during_rotation_and_occlusion(self):
        for instruction in (
            "BODY INTEGRITY THROUGHOUT EVERY FRAME",
            "head, neck, ribcage, spine, pelvis and limbs",
            "occluded by a prop",
            "Do not independently rotate",
            "continuous physically plausible motion",
        ):
            self.assertIn(instruction, MOTION_RULES)

    def test_every_prompt_entry_point_receives_body_integrity_policy(self):
        prompts = [
            build_video_prompt("Two people carry a third person."),
            build_reference_prompt("Turn around.", reference_mode="video"),
            build_reference_prompt("Turn around.", reference_mode="spatial"),
            build_model_video_prompt({"image_path": "start.png"}, "Turn around."),
            build_model_video_prompt({"image_path": "start.png"}, "Turn around.", max_length=20000),
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt[:80]):
                self.assertIn("BODY INTEGRITY THROUGHOUT EVERY FRAME", prompt)
