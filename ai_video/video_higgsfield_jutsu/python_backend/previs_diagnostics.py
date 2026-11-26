"""Build explicit, non-fabricated diagnostics for previs API responses."""
from copy import deepcopy


def _positions(value):
    subjects = value.get('subjects', []) if isinstance(value, dict) else []
    return {s['id']: s.get('position') for s in subjects if isinstance(s, dict) and isinstance(s.get('id'), str)}


def _different(a, b):
    if not (isinstance(a, list) and isinstance(b, list) and len(a) == len(b) == 3):
        return None
    if any(type(v) not in (int, float) for v in a + b):
        return None
    return any(abs(x-y) > 1e-4 for x, y in zip(a, b))


def build_previs_diagnostics(job):
    raw = job.get('rawSceneDirection') or {}
    before = _positions(job.get('sceneDesignBeforeCorrection'))
    original = _positions(raw)
    relations = (job.get('sceneDirection') or {}).get('relations') or []
    quality = job.get('relationQuality') or {}
    after = _positions(job.get('sceneDesign')) if quality.get('status') == 'passed' else {}
    runtime = (job.get('serverSceneCorrection') or {}).get('runtime') or {}
    measured = runtime.get('objectPositions') or {}
    visibility = (job.get('runtimeValidation') or {}).get('visibility') or {}
    visible = {row.get('subject'): row for row in visibility.get('subjects', []) if isinstance(row, dict)}
    traces = []
    for ident in dict.fromkeys([*original, *before, *after, *measured]):
        matches = [r for r in relations if isinstance(r, dict) and r.get('subject') == ident] or [None]
        for relation in matches:
            pos = measured.get(ident) or {}
            actual = pos.get('final')
            initial_motion = pos.get('afterMotionApplied')
            block = visible.get(pos.get('rootName')) or {}
            traces.append({'objectId': ident, 'relation': relation.get('kind') if relation else None,
                'target': relation.get('target') if relation else None,
                'trace': {'aiOriginalPosition': original.get(ident), 'beforeCorrection': before.get(ident),
                          'afterCorrection': after.get(ident), 'blenderCreatedPosition': pos.get('created'),
                          'afterMotionApplied': initial_motion, 'finalPosition': actual,
                          'motionEndPosition': pos.get('motionEnd'), 'sampleFrame': pos.get('sampleFrame'),
                          'measurementStage': 'blender_scene_build_before_render' if pos else None},
                'validation': {'relationSatisfied': None, 'blockedFramesBefore': None,
                               'blockedFramesAfter': block.get('blockedBodyFrames'),
                               'positionOverwritten': _different(initial_motion, actual),
                               'createdDiffersFromPlanned': _different(after.get(ident), pos.get('created'))},
                'aiRetryCount': None})
    corrections = quality.get('corrections') or []
    status = quality.get('status') if relations and quality.get('status') else 'not_checked'
    design = job.get('sceneDesign')
    camera_references = None
    if isinstance(design, dict):
        camera_references = [
            {'subject': track.get('subject'), 'action': track.get('action'),
             'start': track.get('start'), 'end': track.get('end'),
             'handling': 'direction_target' if track.get('action') in ('point', 'reach') else 'reference_only'}
            for track in design.get('tracks', [])
            if isinstance(track, dict) and track.get('target') == 'camera'
        ]
    return {'canonicalNormalization': {'status': job.get('canonicalSceneStatus', 'not_started'),
                                       'changes': deepcopy(job.get('normalizationChanges')),
                                       'errors': deepcopy(job.get('designValidation'))},
            'cameraReferences': camera_references,
            'cameraProjection': deepcopy(runtime.get('projectionWarnings')),
            'pointing': deepcopy(runtime.get('pointingDiagnostics')),
            'facing': deepcopy(runtime.get('facingDiagnostics')),
            'cameraMotionWarnings': deepcopy(runtime.get('cameraMotionWarnings')),
            'sceneState': deepcopy(job.get('sceneState')),
            'sceneAdapter': deepcopy(job.get('sceneAdapter')),
            'sceneAdapterCompletion': deepcopy(job.get('sceneAdapterCompletion')),
            'pythonExtraction': deepcopy(job.get('pythonPromptHints')),
            'aiOriginalObjects': deepcopy(raw.get('subjects')) if isinstance(raw, dict) else None,
            'pythonAddedObjects': deepcopy(job.get('objectUnion')),
            'relations': deepcopy(relations),
            'relationProvenance': {'status': 'not_implemented', 'priority': None},
            'relationConflictCheck': {'status': status, 'error': quality.get('error')},
            'objectTraces': traces,
            'occlusionCorrection': {'status': 'not_performed', 'reason': '가림 자동 보정 기능 미구현'},
            'revalidation': {'status': 'passed' if corrections and quality.get('status') == 'passed' else 'not_performed',
                             'scope': '관계 보정 후 좌표 재검사' if corrections else None},
            'dependencyRecalculation': {'status': 'performed' if corrections and 'dependencyCorrections' in quality else 'not_performed',
                                        'changes': deepcopy(quality.get('dependencyCorrections')),
                                        'graph': deepcopy(quality.get('dependencies'))},
            'runtimeValidation': deepcopy(job.get('runtimeValidation'))}
