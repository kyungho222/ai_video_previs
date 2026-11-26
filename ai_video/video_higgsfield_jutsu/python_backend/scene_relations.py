"""Coordinate-only relative constraints; unsupported relations fail explicitly."""
from copy import deepcopy
import math
import re
from .resources.scene_props import CATALOG, scene_prop_parts

class RelationError(ValueError):
    pass

_RELATIONS = {'left_of': (0, -1), 'right_of': (0, 1), 'behind': (1, -1), 'in_front_of': (1, 1)}


def prepare_initial_contacts(design, relations, request):
    """Resolve unambiguous already-seated starts without inventing spatial relations."""
    result, kept, changes = deepcopy(design), deepcopy(relations), []
    people = [s for s in result.get('subjects', []) if s.get('kind') == 'human']
    # With several people, an unqualified sentence does not identify who sits.
    if len(people) != 1:
        return result, kept, {'changes': changes}
    sentences = re.split(r'[.!?\n]+', str(request or ''))
    seated = any(re.search(r'(?:앉아|앉은\s*(?:채|상태)|앉아\s*있|\bseated\b|\bsitting\b)', s, re.I)
                 and not re.search(r'(?:앉기\s*전|앉으려|앉았다가|앉아\s*일어나|sit\s+down)', s, re.I)
                 for s in sentences)
    if not seated:
        return result, kept, {'changes': changes}
    actor = people[0]
    tracks = [t for t in result.get('tracks', []) if t.get('subject') == actor.get('id') and t.get('action') == 'sit' and t.get('start') == 0]
    if len(tracks) != 1:
        return result, kept, {'changes': changes}
    track = tracks[0]
    if not any(s.get('id') == track.get('target') and s.get('kind') == 'prop' for s in result.get('subjects', [])):
        return result, kept, {'changes': changes}
    related = [r for r in kept if isinstance(r, dict) and r.get('subject') == actor['id'] and r.get('target') == track['target']]
    directional = re.compile(r'앞|뒤|왼쪽|오른쪽|좌측|우측|\b(?:front|behind|left|right)\b', re.I)
    # Preserve any actual positional instruction; it may require a transition.
    if any(directional.search(str(r.get('evidence') or '')) for r in related):
        return result, kept, {'changes': changes}
    for r in related:
        if r.get('scope') == 'initial' and r.get('evidence') and r['evidence'] in str(request or ''):
            kept.remove(r)
            changes.append({'relation': r, 'reason': 'contact_evidence_does_not_specify_relative_position'})
        else:
            return deepcopy(design), deepcopy(relations), {'changes': []}
    before = actor['position'][:]
    actor['position'] = track['position'][:]
    track['initialPosture'] = True
    changes.append({'subject': actor['id'], 'field': 'initialPosture', 'before': before,
                    'after': actor['position'][:], 'reason': 'explicit_initial_seated_state'})
    return result, kept, {'changes': changes}


def build_anchor_catalog(design):
    """Approximate local-space anchors, not evaluated Blender mesh geometry."""
    catalog = {}
    for obj in design.get('subjects', []):
        ident = obj.get('id')
        if not ident:
            continue
        if obj.get('kind') == 'human':
            points = {'feet': [0, 0, 0], 'pelvis': [0, 0, .93],
                      'left_hand': [-.32, 0, .94], 'right_hand': [.32, 0, .94],
                      'head': [0, 0, 1.76]}
        else:
            points = {'base': [0, 0, 0], 'center': [0, 0, .5], 'top': [0, 0, 1]}
            parts = scene_prop_parts(obj.get('label'))
            if parts and any(parts == CATALOG[key][1] for key in ('chair', 'bench', 'sofa', 'bed')):
                points['seat'] = [0, 0, parts[0][1][2] + parts[0][2][2] / 2]
        catalog[ident] = {'source': 'estimated', 'coordinateSystem': 'local_normalized' if obj.get('kind') != 'human' else 'local_rig', 'points': points}
    return catalog


def build_dependency_graph(design, relations):
    objects = {obj['id']: [] for obj in design.get('subjects', []) if obj.get('id')}
    def add(parent, child):
        if parent in objects and child in objects and child not in objects[parent]:
            objects[parent].append(child)
    for relation in relations:
        if isinstance(relation, dict):
            add(relation.get('target'), relation.get('subject'))
    for track in design.get('tracks', []):
        add(track.get('target'), track.get('subject'))
    cameras = {str(i): list(scene.get('camera', {}).get('focus') or [])
               for i, scene in enumerate(design.get('scenes', []))}
    return {'objects': objects, 'cameras': cameras}


def derive_movement_limits(text, design):
    # Only lock human roots when the user explicitly requires people to stay put.
    if re.search(r'(?:인물|캐릭터|멤버|사람).{0,20}위치.{0,12}(?:고정|유지)', str(text or ''), re.S):
        return {obj['id']: {'locked': True} for obj in design.get('subjects', [])
                if obj.get('kind') == 'human' and obj.get('id')}
    return {}


def apply_relations(design, relations, *, limits=None):
    result = deepcopy(design)
    subjects = {s.get('id'): s for s in result.get('subjects', [])}
    tracks = result.get('tracks', [])
    corrections = []
    limits = limits or {}
    origins = {ident: obj['position'][:] for ident, obj in subjects.items()}
    constraints = []
    for r in relations:
        if not isinstance(r, dict):
            raise RelationError('객체 관계가 올바른 형식이 아닙니다.')
        a, b, kind, scope = r.get('subject'), r.get('target'), r.get('kind'), r.get('scope')
        if a not in subjects or b not in subjects or a == b:
            raise RelationError('객체 관계의 대상 ID가 없거나 중복됩니다: ' + str((a, b)))
        if kind not in _RELATIONS or scope not in ('initial', 'entire'):
            raise RelationError('지원하지 않는 객체 관계 또는 적용 구간입니다: ' + str((kind, scope)))
        axis, sign = _RELATIONS[kind]
        reference = r.get('referenceFrame', 'world')
        direction = [1. if i == axis else 0. for i in range(3)]
        if reference == 'screen':
            shots = result.get('scenes', [])
            if not shots:
                raise RelationError('화면 관계를 계산할 기준 카메라가 없습니다.')
            angle = math.radians(shots[0]['camera'].get('azimuth', 0))
            direction = [-math.cos(angle), math.sin(angle), 0] if axis == 0 else [math.sin(angle), math.cos(angle), 0]
        elif reference == 'object':
            angle = math.radians(subjects[b].get('yaw', 0))
            direction = [math.cos(angle), math.sin(angle), 0] if axis == 0 else [-math.sin(angle), math.cos(angle), 0]
        elif reference != 'world':
            raise RelationError('지원하지 않는 공간 기준입니다: ' + str(reference))
        constraints.append((a, b, direction, sign, scope, kind, reference))
    def points(key, scope):
        values = [subjects[key]['position']]
        if scope == 'entire':
            values += [t['position'] for t in tracks if t.get('subject') == key and isinstance(t.get('position'), list)]
        return values
    for _ in range(max(1, len(constraints) * 4)):
        changed = False
        for a, b, direction, sign, scope, kind, reference in constraints:
            ap, bp = points(a, scope), points(b, scope)
            # A conservative envelope keeps a persistent relation across all keyframes.
            half_a = sum(abs(d)*v for d,v in zip(direction, subjects[a].get('size', [1,1,1]))) / 2
            half_b = sum(abs(d)*v for d,v in zip(direction, subjects[b].get('size', [1,1,1]))) / 2
            def projection(p): return sum(d*v for d,v in zip(direction,p))
            gap = max(.2, half_a + half_b + .1)
            limit = (min(projection(p) for p in bp) - gap) if sign < 0 else (max(projection(p) for p in bp) + gap)
            extreme = max(projection(p) for p in ap) if sign < 0 else min(projection(p) for p in ap)
            delta = min(0, limit - extreme) if sign < 0 else max(0, limit - extreme)
            if abs(delta) > 1e-7:
                policy = limits.get(a, {})
                if policy.get('locked'):
                    raise RelationError('고정된 객체는 관계 보정을 위해 이동할 수 없습니다: ' + str(a))
                proposed = subjects[a]['position'][:]
                proposed = [v+d*delta for v,d in zip(proposed,direction)]
                distance = math.dist(proposed, origins[a])
                maximum = policy.get('maxTranslation')
                if maximum is not None and (type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum < 0 or distance > maximum + 1e-7):
                    raise RelationError('객체 관계 보정이 허용 이동 범위를 초과합니다: ' + str(a))
                for p in ap:
                    p[:] = [v+d*delta for v,d in zip(p,direction)]
                    if any(not math.isfinite(v) or abs(v) > 50 for v in p):
                        raise RelationError('객체 관계를 허용 좌표 범위에서 만족할 수 없습니다: ' + str((a,b,kind)))
                corrections.append({'subject': a, 'target': b, 'kind': kind, 'direction': direction, 'referenceFrame': reference, 'offset': round(delta,4)})
                changed = True
        if not changed:
            return result, {'relations': deepcopy(relations), 'corrections': corrections, 'status': 'passed',
                            'anchors': build_anchor_catalog(result),
                            'dependencies': build_dependency_graph(result, relations), 'limits': deepcopy(limits)}
    raise RelationError('객체 관계가 서로 충돌하거나 보정 후에도 유지되지 않습니다.')


def reconcile_relations(design, relations, *, limits=None):
    """Apply relations, recompute known dependent poses and recheck constraints."""
    from .scene_correction import correct_scene
    adjusted, report = apply_relations(design, relations, limits=limits)
    if not report['corrections']:
        report['dependencyCorrections'] = []
        return adjusted, report
    dependent, dependent_report = correct_scene(adjusted)
    _, recheck = apply_relations(dependent, relations, limits=limits)
    if recheck['corrections']:
        raise RelationError('의존 동작을 재계산하면 객체 관계가 다시 위반됩니다.')
    report['dependencyCorrections'] = dependent_report['changes']
    report['dependencyWarnings'] = dependent_report['warnings']
    report['anchors'] = build_anchor_catalog(dependent)
    report['dependencies'] = build_dependency_graph(dependent, relations)
    return dependent, report
