import ast
import json
import unittest
from unittest.mock import AsyncMock
from ai_video.video_higgsfield_jutsu.python_backend.code_validation import validate_generated_code

class MarkerContractTests(unittest.TestCase):
    def marker_issues(self, code):
        return [i for i in validate_generated_code(code) if i['code'] == 'MOTION_MARKERS']

    def test_literal_comprehension_extra_keys(self):
        self.assertTrue(self.marker_issues("markers = {k: parts[k] for k in ('pelvis', 'hand_l', 'hand_r', 'knee_l', 'knee_r')}\nprevis_register_motion(root, markers)"))

    def test_update_retains_extra_keys(self):
        code = "markers = {k: parts[k] for k in ('pelvis', 'hand_l', 'hand_r', 'knee_l', 'knee_r')}\nmarkers.update({'L_Hand': lh, 'R_Hand': rh, 'L_Knee': lk, 'R_Knee': rk})\nprevis_register_motion(root, markers)"
        self.assertTrue(self.marker_issues(code))

    def test_exact_mapping_and_comprehension_pass(self):
        for value in ("{'L_Hand': lh, 'R_Hand': rh, 'L_Knee': lk, 'R_Knee': rk}", "{k: parts[k] for k in ('L_Hand','R_Hand','L_Knee','R_Knee')}"):
            self.assertFalse(self.marker_issues('markers = ' + value + '\nprevis_register_motion(root, markers)'))

    def test_unknown_helper_mapping_remains_supported(self):
        self.assertFalse(self.marker_issues('markers = get_actual_markers(root)\nprevis_register_motion(root, markers)'))

    def test_required_helper_arguments_checked(self):
        for code in ('previs_register_motion(root)', 'previs_camera_register_subjects(camera)',
                     'previs_camera_register_subjects(camera, subjects, required_markers=[])'):
            self.assertTrue(any(i['code'] == 'PREVIS_API_ARGUMENTS' for i in validate_generated_code(code)))

    def test_keyword_helper_arguments_pass(self):
        self.assertFalse(validate_generated_code("previs_camera_register_subjects(camera=camera, subjects=subjects)"))

    def test_contract_uses_actual_signatures(self):
        from ai_video.video_higgsfield_jutsu.python_backend.code_validation import previs_api_contract
        contract = json.loads(previs_api_contract())
        motion = contract['functions']['previs_register_motion']
        self.assertEqual(motion['requiredArguments'], ['root', 'markers'])
        self.assertEqual(set(motion['markers']['requiredKeys']), {'L_Hand','R_Hand','L_Knee','R_Knee'})
        self.assertFalse(motion['markers']['additionalKeys'])
        self.assertIn('required_markers=None', motion['signature'])

class PromptContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_receives_contract_even_with_only_runtime_error(self):
        from ai_video.video_higgsfield_jutsu.python_backend.previs_workers import repair_previs_functions
        requests = []
        async def request(path, body, **kwargs):
            requests.append(body)
            return {'output_text': json.dumps({'replacements':[{'name':'build_subjects','code':'def build_subjects(scene):\n    return []'}]})}
        await repair_previs_functions(request, 'def build_subjects(scene):\n    return []', [], 'test', failure_text='in build_subjects')
        self.assertEqual(len(requests), 1)
        context=json.loads(requests[0]['input'][0]['content'])
        self.assertIn('previs_register_motion', context['apiContract']['functions'])

    async def test_both_workers_receive_contract(self):
        from unittest.mock import patch
        from ai_video.video_higgsfield_jutsu.python_backend.previs_workers import run_previs_workers
        requests = []
        async def request(path, body, **kwargs):
            requests.append(body)
            name = body['text']['format']['name']
            if name == 'previs_director_plan':
                data = {'scene_action':'Dance', 'camera':'Orbit', 'motion_detail':'basic_motion',
                        'timeline':[{'start':0,'end':10,'subject_action':'Dance','camera_goal':'Orbit'}],
                        'preprocessed_prompt':{k:['Dance'] for k in ['scene','choreography','motion','camera','mood_style']}}
            else:
                data = {'code':'def generated():\n    pass', 'notes':''}
            return {'output':[{'content':[{'type':'output_text','text':json.dumps(data)}]}]}
        with patch('ai_video.video_higgsfield_jutsu.python_backend.previs_workers.validate_worker', return_value=[]):
            await run_previs_workers(request, {'id':'test','duration':10,'aspect':'16:9'}, [], lambda *args:None, 'test')
        workers=[r for r in requests if r['text']['format']['name'] != 'previs_director_plan']
        self.assertEqual(len(workers),2)
        for body in workers:
            self.assertIn('PREVIS_API_CONTRACT',body['instructions'])
            context=json.loads(body['input'][0]['content'])
            self.assertFalse(context['apiContract']['functions']['previs_register_motion']['markers']['additionalKeys'])

    async def test_main_instruction_contains_same_contract(self):
        from ai_video.video_higgsfield_jutsu.python_backend.cloud_python import instructions
        self.assertIn('PREVIS_API_CONTRACT', instructions({'duration':10,'aspect':'16:9'}))

if __name__ == '__main__':
    unittest.main()
