import ast
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_video.video_higgsfield_jutsu.python_backend import previs_workers as workers
from ai_video.video_higgsfield_jutsu.python_backend.previs_finalization import finalize_current_previs
from ai_video.video_higgsfield_jutsu.python_backend.previs_execution import (
    resolves_validation_failure, record_validation_failure, resolve_validation_failures,
)
from ai_video.video_higgsfield_jutsu.python_backend.usage_history import UsageHistory

BACKEND = Path(workers.__file__).parent


def valid_plan():
    return {
        'scene_action': 'Two people wave', 'camera': 'Orbit', 'motion_detail': 'basic_motion',
        'timeline': [{'start': 0, 'end': 10, 'subject_action': 'Wave', 'camera_goal': 'Orbit'}],
        'preprocessed_prompt': {key: ['Wave'] for key in workers.PROMPT_SECTIONS},
    }


def response(value):
    text = value if isinstance(value, str) else json.dumps(value)
    return {'output': [{'content': [{'type': 'output_text', 'text': text}]}]}


class DirectorPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, candidates):
        self.calls, self.events = [], []
        self.job = {'id': 'test', 'duration': 10, 'aspect': '16:9'}
        self.messages = [{'role': 'user', 'content': 'Two people wave, ten seconds.'}]
        async def request(path, body, **kwargs):
            self.calls.append(copy.deepcopy(body))
            if body['text']['format']['name'] == 'previs_director_plan':
                value = candidates.pop(0)
                return value if isinstance(value, dict) and 'output' in value else response(value)
            return response({'code': 'def generated():\n    pass', 'notes': ''})
        with patch.object(workers, 'validate_worker', return_value=[]):
            return await workers.run_previs_workers(request, self.job, self.messages,
                lambda *event: self.events.append(event), 'google/gemma-test')

    async def test_three_failures_then_gpt_receives_failed_candidate_and_error(self):
        bad = valid_plan()
        bad['timeline'][0]['end'] = 7
        await self.execute([copy.deepcopy(bad) for _ in range(3)] + [valid_plan()])
        director = self.calls[:4]
        self.assertEqual([r['model'] for r in director], ['google/gemma-test'] * 3 + ['gpt-6.1-terra'])
        self.assertIn('"end": 7', director[-1]['input'][-2]['content'])
        self.assertIn(self.job['directorAttempts'][2]['error'], director[-1]['input'][-1]['content'])
        self.assertEqual(len(self.job['directorAttempts']), 4)
        self.assertTrue(self.job['directorAttempts'][-1]['passed'])
        self.assertEqual(self.messages, [{'role': 'user', 'content': 'Two people wave, ten seconds.'}])
        self.assertTrue(all(call['model'] == 'google/gemma-test' for call in self.calls[4:]))

    async def test_valid_first_plan_needs_no_fallback(self):
        await self.execute([valid_plan()])
        self.assertEqual(len(self.job['directorAttempts']), 1)
        self.assertEqual(len(self.calls), 3)

    async def test_invalid_json_is_retained_and_supplied_for_repair(self):
        await self.execute(['{broken-json', valid_plan()])
        self.assertEqual(self.job['directorAttempts'][0]['candidate'], '{broken-json')
        self.assertEqual(self.calls[1]['input'][-2]['content'], '{broken-json')

    async def test_all_four_invalid_stop_before_worker_generation(self):
        with self.assertRaisesRegex(RuntimeError, '마지막 원인:'):
            await self.execute([{} for _ in range(4)])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.job['directorAttempts'][-1]['action'], 'stop')
        self.assertNotIn('workerDrafts', self.job)

    async def test_missing_prompt_sections_also_repair(self):
        bad = valid_plan()
        bad['preprocessed_prompt'].pop('motion')
        await self.execute([bad, valid_plan()])
        self.assertFalse(self.job['directorAttempts'][0]['passed'])

    async def test_safety_refusal_never_retried_with_gpt(self):
        refusal = {'output': [{'content': [{'type': 'refusal', 'refusal': 'blocked'}]}]}
        with self.assertRaisesRegex(RuntimeError, '안전 정책'):
            await self.execute([refusal])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.job['directorAttempts'][0]['category'], 'execution_safety')


class RenderPolicyTests(unittest.TestCase):
    def gate(self, error, *, allow=True):
        tree = ast.parse((BACKEND / 'resources/cloud_prelude.py').read_text(encoding='utf-8-sig'))
        quality_type = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PrevisQualityError')
        render = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'render_previs')
        gate = next(n for n in render.body if isinstance(n, ast.FunctionDef) and n.name == 'quality_check')
        ns = {'allow_quality_warnings': allow, 'validation_warnings': [], '_jutsu_motion_requirements': {}}
        exec(compile(ast.Module(body=[quality_type, gate], type_ignores=[]), '<gate>', 'exec'), ns)
        def validate(_):
            raise ns['PrevisQualityError']('camera strength') if error == 'quality' else error
        result = ns['quality_check']('camera', validate)
        return result, ns['validation_warnings']

    def test_quality_warning_continues(self):
        result, warnings = self.gate('quality')
        self.assertFalse(result['passed'])
        self.assertEqual(warnings[0]['action'], 'continue_with_warning')

    def test_strict_quality_mode_remains_available(self):
        with self.assertRaisesRegex(RuntimeError, 'camera strength'):
            self.gate('quality', allow=False)

    def test_structural_and_execution_errors_never_suppressed(self):
        for error in (RuntimeError('missing mesh'), ValueError('invalid configuration'),
                      TypeError('invalid code'), PermissionError('forbidden')):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                self.gate(error)

    def test_motion_quality_does_not_hide_missing_markers(self):
        source = (BACKEND / 'resources/motion_library.py').read_text(encoding='utf-8-sig')
        self.assertIn('raise PrevisQualityError(', source)
        self.assertIn('raise RuntimeError("Motion validation configuration invalid: required joint marker object missing")', source)

    def motion_validator(self, roots, objects=None):
        import math
        from types import SimpleNamespace
        tree = ast.parse((BACKEND / 'resources/motion_library.py').read_text(encoding='utf-8-sig'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'previs_validate_scene_motion')
        class QualityError(RuntimeError):
            pass
        class Objects(list):
            def get(self, name):
                return (objects or {}).get(name)
        scene = SimpleNamespace(frame_current=1, frame_start=1, frame_end=24, frame_set=lambda frame: None)
        bpy = SimpleNamespace(context=SimpleNamespace(scene=scene), data=SimpleNamespace(objects=Objects(roots)))
        ns = {'PrevisQualityError': QualityError, '_motion_math': math}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<motion>', 'exec'), ns)
        with patch.dict('sys.modules', {'bpy': bpy}):
            return ns['previs_validate_scene_motion']({'articulated': True, 'member_count': 1})

    def test_single_missing_rig_is_structural(self):
        with self.assertRaisesRegex(RuntimeError, 'human rigs, found 0') as caught:
            self.motion_validator([])
        self.assertEqual(type(caught.exception), RuntimeError)

    def test_partial_joint_action_only_requires_registered_joint(self):
        from types import SimpleNamespace
        class Root(dict):
            name = 'person'
            children = [SimpleNamespace(name='person_L_Hand', location=(0, 0, 0))]
        root = Root(previs_motion_version='registered', previs_required_motion_markers='["L_Hand"]')
        with self.assertRaisesRegex(RuntimeError, 'Articulated motion validation failed') as caught:
            self.motion_validator([root])
        # Actual joint exists but is static: a quality issue, not missing anatomy.
        self.assertEqual(type(caught.exception).__name__, 'QualityError')


class RepairCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.failure = {'id': 'failure-1', 'tool': 'run_python', 'projectId': 'scene-1',
                        'code': 'def build_subjects(scene):\n    return missing\nbuild_subjects(scene)', 'operationId': 'build-1'}

    def test_import_or_unrelated_query_never_resolves_failed_mutation(self):
        for kind in ('import_asset', 'query_python'):
            self.assertFalse(resolves_validation_failure(self.failure, kind, {'projectId': 'scene-1'}))

    def test_successful_other_project_does_not_resolve_failure(self):
        self.assertFalse(resolves_validation_failure(self.failure, 'run_python',
            {'projectId': 'scene-2', 'code': self.failure['code']}, 'failure-1'))

    def test_unrelated_mutation_does_not_resolve_failure(self):
        self.assertFalse(resolves_validation_failure(self.failure, 'run_python',
            {'projectId': 'scene-1', 'code': 'pass'}))

    def test_corrected_executed_build_resolves_failure(self):
        self.assertTrue(resolves_validation_failure(self.failure, 'run_python',
            {'projectId': 'scene-1', 'code': 'def build_subjects(scene):\n    return []\nbuild_subjects(scene)'}))

    def test_definition_without_execution_does_not_resolve_failure(self):
        self.assertFalse(resolves_validation_failure(self.failure, 'run_python',
            {'projectId': 'scene-1', 'code': 'def build_subjects(scene):\n    return []'}))

    def test_explicit_backend_repair_correlation_resolves_snippet(self):
        self.assertTrue(resolves_validation_failure(self.failure, 'run_python',
            {'projectId': 'scene-1', 'code': 'fixed_call()'}, 'failure-1'))

    def test_anonymous_query_cannot_clear_previous_query(self):
        failure = {**self.failure, 'tool': 'query_python', 'operationId': None}
        self.assertFalse(resolves_validation_failure(failure, 'query_python', {'projectId': 'scene-1'}))

    def test_exact_query_retry_with_new_id_can_resolve_failure(self):
        failure = {**self.failure, 'tool': 'query_python', 'code': 'print(scene.camera)', 'operationId': 'old'}
        self.assertTrue(resolves_validation_failure(failure, 'query_python',
            {'projectId': 'scene-1', 'code': 'print(scene.camera)', 'operationId': 'new'}))

    def test_repairing_second_failure_does_not_discard_first(self):
        job = {}
        first = record_validation_failure(job, {**self.failure, 'id': 'A'})
        second = record_validation_failure(job, {**self.failure, 'id': 'B'})
        resolve_validation_failures(job, 'run_python', {'projectId': 'scene-1', 'code': 'fixed_call()'}, second['id'])
        self.assertEqual(job['unresolvedValidation'], [first])

    def test_internal_repair_metadata_is_not_appended_to_ai_messages(self):
        source = (BACKEND / 'previs_execution.py').read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        run = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'run')
        extend = next(n for n in ast.walk(run) if isinstance(n, ast.Expr)
                      and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
                      and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id == 'messages'
                      and n.value.func.attr == 'extend')
        ns = {'messages': [], 'output': [{'type': 'function_call', 'call_id': 'abc', 'validationRepairFor': 'internal'}]}
        exec(compile(ast.Module(body=[extend], type_ignores=[]), '<extend>', 'exec'), ns)
        self.assertEqual(ns['messages'], [{'type': 'function_call', 'call_id': 'abc'}])

    def test_execution_compares_unwrapped_code_for_query_recovery(self):
        tree = ast.parse((BACKEND / 'previs_execution.py').read_text(encoding='utf-8-sig'))
        invocation = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name) and n.func.id == 'resolve_validation_failures')
        ns = {'arguments': {'projectId': 'scene-1', 'code': 'injected helpers; print(scene.camera)'},
              'submitted_code': 'print(scene.camera)'}
        value = eval(compile(ast.Expression(body=invocation.args[2]), '<resolve-args>', 'eval'), ns)
        job = {'unresolvedValidation': [{'tool': 'query_python', 'projectId': 'scene-1',
                                        'code': 'print(scene.camera)', 'id': 'failed-query'}]}
        resolve_validation_failures(job, 'query_python', value)
        self.assertNotIn('unresolvedValidation', job)


class FinalizationPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_code_error_cannot_return_old_video(self):
        job = {'media': [{'mediaType': 'video/mp4'}],
               'unresolvedValidation': {'category': 'execution_safety', 'error': 'missing joint'}}
        with self.assertRaisesRegex(RuntimeError, 'missing joint'):
            await finalize_current_previs(None, job, [], lambda *args: None)

    async def test_valid_existing_video_still_returns(self):
        media = [{'mediaType': 'video/mp4', 'url': '/saved'}]
        self.assertEqual((await finalize_current_previs(None, {'media': media}, [], lambda *a: None))['media'], media)


class HistoryPolicyTests(unittest.TestCase):
    def test_failed_planning_diagnostics_survive_history(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            history = UsageHistory(Path(directory) / 'history.sqlite3')
            job = {'id': 'aebbffc4-41de-4252-9eed-e858dece218f',
                   'directorAttempts': [{'candidate': '{}', 'error': 'missing timeline', 'passed': False}]}
            history.save_previs(job, 'failed')
            self.assertEqual(history.get(job['id'])['directorAttempts'], job['directorAttempts'])


if __name__ == '__main__':
    unittest.main()
