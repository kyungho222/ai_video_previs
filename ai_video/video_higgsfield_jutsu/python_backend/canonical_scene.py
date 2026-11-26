"""Provider-neutral scene normalization. No inference from the model identifier."""
from copy import deepcopy
import math
import re
from .resources.scene_props import CATALOG, scene_prop_parts
from .scene_contract import ACTIONS, KINDS, action_channel

class CanonicalSceneError(ValueError):
    pass

_ACTIONS = {'walking':'walk', 'running':'run', 'speaking':'speak', 'talking':'speak',
            'sitting':'sit', 'crouching':'crouch', 'pointing':'point', 'waving':'wave'}
_KINDS = {'person':'human', 'object':'prop'}
_UNITS = {'m': 1, 'meter': 1, 'metre': 1, 'cm': .01, 'mm': .001}


def normalize_scene(value):
    if not isinstance(value, dict):
        raise CanonicalSceneError('장면 응답은 JSON 객체여야 합니다.')
    result = deepcopy(value)
    changes = []
    from .scene_direction import DIRECTION_SCHEMA
    def restore_paths(row, schema, path=''):
        if isinstance(row, list):
            for i, child in enumerate(row):
                restore_paths(child, schema.get('items', {}), f'{path}[{i}]')
        elif isinstance(row, dict):
            props = schema.get('properties', {})
            for key in list(row):
                if '.' not in key or key in props:
                    continue
                parts = key.split('.')
                spec = schema
                valid = True
                for part in parts:
                    if part not in spec.get('properties', {}):
                        valid = False
                        break
                    spec = spec['properties'][part]
                if not valid:
                    # A root field accidentally qualified with a known container.
                    if len(parts) == 2 and parts[0] in props and props[parts[0]].get('type') == 'object' and parts[-1] in props:
                        parts = [parts[-1]]
                    else:
                        continue
                node = row
                for part in parts[:-1]:
                    if part not in node:
                        node[part] = {}
                    if not isinstance(node[part], dict):
                        raise CanonicalSceneError(f'{path}.{key}: 중첩 필드 형식 충돌')
                    node = node[part]
                leaf = parts[-1]
                if leaf in node and node[leaf] != row[key]:
                    raise CanonicalSceneError(f'{path}.{key}: 중첩 필드 값 충돌')
                node[leaf] = row.pop(key)
                changes.append({'path':path+'.'+key, 'targetPath':path+'.'+'.'.join(parts),
                                'reason':'restore_schema_field_path'})
            for key, child in row.items():
                if key in props:
                    restore_paths(child, props[key], path+'.'+key if path else key)
    restore_paths(result, DIRECTION_SCHEMA)
    for index, relation in enumerate(result.get('relations', []) if isinstance(result.get('relations'), list) else []):
        if isinstance(relation, dict) and 'referenceFrame' not in relation:
            relation['referenceFrame'] = 'screen'
            changes.append({'path':f'relations[{index}].referenceFrame', 'after':'screen',
                            'reason':'default_initial_camera_reference'})
    def color(row, key, path):
        rgb = row.get(key)
        if (isinstance(rgb, list) and len(rgb) == 3
                and all(type(n) in (int, float) and math.isfinite(n) and n == int(n) and 0 <= n <= 255 for n in rgb)
                and any(n > 1 for n in rgb)):
            row[key] = [n / 255 for n in rgb]
            changes.append({'path':path, 'before':rgb, 'after':row[key], 'reason':'rgb_byte_to_unit'})
    color(result, 'background', 'background')
    units = result.pop('units', None)
    if units is not None:
        if not isinstance(units, dict) or set(units) - {'position'}:
            raise CanonicalSceneError('지원하지 않는 좌표 단위 정보입니다.')
        unit = units.get('position')
        if not isinstance(unit, str) or unit.lower() not in _UNITS:
            raise CanonicalSceneError('지원하지 않는 좌표 단위입니다.')
        scale = _UNITS[unit.lower()]
        changes.append({'path':'units.position', 'before':unit, 'after':'m', 'reason':'explicit_unit_conversion'})
    else:
        scale = 1
    def position(row, path):
        if not isinstance(row, dict) or 'position' not in row:
            return
        vector = row['position']
        if not isinstance(vector, list) or len(vector) != 3 or any(type(n) not in (int,float) or not math.isfinite(n) for n in vector):
            raise CanonicalSceneError(f'{path}.position: 유한한 3차원 좌표가 필요합니다.')
        if scale != 1:
            row['position'] = [n * scale for n in vector]
            changes.append({'path':path+'.position', 'before':vector, 'after':row['position'], 'reason':'explicit_unit_conversion'})
        if row['position'][2] < 0:
            before = row['position'][:]
            row['position'] = [*before[:2], 0]
            changes.append({'path':path+'.position', 'before':before, 'after':row['position'][:],
                            'reason':'ground_root_position'})
    subjects = result.get('subjects', [])
    if not isinstance(subjects, list):
        raise CanonicalSceneError('subjects는 배열이어야 합니다.')
    for index, item in enumerate(subjects):
        if not isinstance(item, dict):
            raise CanonicalSceneError('객체 항목은 JSON 객체여야 합니다.')
        position(item, f'subjects[{index}]')
        color(item, 'color', f'subjects[{index}].color')
        kind = item.get('kind')
        if isinstance(kind, str):
            normalized = _KINDS.get(kind.casefold(),kind.casefold())
            if normalized not in KINDS:
                raise CanonicalSceneError(f'subjects[{index}].kind: 지원하지 않는 객체 유형 {kind}')
            if normalized != kind:
                item['kind'] = normalized
                changes.append({'path':f'subjects[{index}].kind','before':kind,'after':normalized,'reason':'object_type_alias'})
    beats = result.get('beats', [])
    if not isinstance(beats, list):
        raise CanonicalSceneError('beats는 배열이어야 합니다.')
    for index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            raise CanonicalSceneError('연출 구간은 JSON 객체여야 합니다.')
        actions = beat.get('actions', [])
        if not isinstance(actions, list):
            raise CanonicalSceneError(f'beats[{index}].actions는 배열이어야 합니다.')
        for action_index, action in enumerate(actions):
            if not isinstance(action, dict):
                raise CanonicalSceneError('동작 항목은 JSON 객체여야 합니다.')
            path = f'beats[{index}].actions[{action_index}]'
            target = action.get('target')
            if isinstance(target, dict):
                target_type, target_id = target.get('type'), target.get('id')
                if target_type == 'camera' and target_id in (None, 'camera', 'active_camera', 'CAM_main'):
                    action['target'] = 'camera'
                    changes.append({'path':path+'.target', 'before':target, 'after':'camera', 'reason':'explicit_camera_reference'})
                elif target_type == 'object' and isinstance(target_id, str) and any(item.get('id') == target_id for item in subjects):
                    action['target'] = target_id
                    changes.append({'path':path+'.target', 'before':target, 'after':target_id, 'reason':'explicit_object_reference'})
                else:
                    raise CanonicalSceneError(f'{path}.target: 지원하지 않는 참조 {target_id}')
            position(action,path)
            name = action.get('action')
            if isinstance(name, str):
                normalized = _ACTIONS.get(name.casefold(),name.casefold())
                if normalized not in ACTIONS:
                    raise CanonicalSceneError(f'{path}.action: 지원하지 않는 동작 {name}')
                if normalized != name:
                    action['action'] = normalized
                    changes.append({'path':path+'.action','before':name,'after':normalized,'reason':'action_alias'})
    for index, beat in enumerate(beats):
        actions = beat.get('actions', [])
        roots = {}
        for action in actions:
            if action_channel(action.get('action')) in ('movement', 'posture'):
                roots.setdefault(action.get('subject'), []).append(action)
        for action_index, action in enumerate(actions):
            candidates = roots.get(action.get('subject'), [])
            if action_channel(action.get('action')) not in ('gesture', 'vocal') or len(candidates) != 1:
                continue
            root = candidates[0]
            if not isinstance(root.get('position'), list) or type(root.get('yaw')) not in (int, float):
                continue
            if action.get('position') != root['position'] or action.get('yaw') != root['yaw']:
                before = {'position':deepcopy(action.get('position')), 'yaw':action.get('yaw')}
                action['position'], action['yaw'] = root['position'][:], root['yaw']
                changes.append({'path':f'beats[{index}].actions[{action_index}]',
                                'before':before,'after':{'position':action['position'][:],'yaw':action['yaw']},
                                'reason':'overlay_inherits_root_channel'})
    for index, beat in enumerate(beats):
        if beat.get('purpose') == 'wait' or beat.get('timingEvidence'):
            continue
        actions = beat.get('actions', [])
        kept = []
        for action_index, action in enumerate(actions):
            redundant = (action.get('action') == 'idle' and action.get('role') != 'speaker'
                and action.get('target') is None and any(
                    other is not action and other.get('subject') == action.get('subject')
                    and other.get('action') != 'idle'
                    and action_channel(other.get('action')) in ('movement', 'posture')
                    and other.get('position') == action.get('position')
                    and other.get('yaw') == action.get('yaw')
                    for other in actions))
            if redundant:
                changes.append({'path':f'beats[{index}].actions[{action_index}]', 'before':deepcopy(action),
                                'after':None, 'reason':'redundant_idle_with_root_action'})
            else:
                kept.append(action)
        beat['actions'] = kept
    # Resolve a missing sit target only when the beat explicitly mentions a seat
    # and the authored scene has exactly one compatible named seat.
    for index, beat in enumerate(beats):
        intent = beat.get('intent', '')
        if not isinstance(intent, str) or not re.search(r'앉|sits?|sitting', intent, re.I):
            continue
        candidates = []
        for seat in subjects:
            if seat.get('kind') != 'prop' or not isinstance(seat.get('id'), str):
                continue
            parts = scene_prop_parts(seat.get('label'))
            for key in ('chair', 'bench', 'sofa', 'bed'):
                aliases, geometry = CATALOG[key]
                if parts == geometry and any(re.search(re.escape(alias), intent, re.I) for alias in aliases):
                    candidates.append(seat['id'])
                    break
        if len(candidates) == 1:
            for action_index, action in enumerate(beat.get('actions', [])):
                if isinstance(action, dict) and action.get('action') == 'sit' and action.get('target') is None:
                    action['target'] = candidates[0]
                    changes.append({'path':f'beats[{index}].actions[{action_index}].target',
                                    'before':None, 'after':candidates[0], 'reason':'unique_explicit_seat_reference'})
    return result, changes
