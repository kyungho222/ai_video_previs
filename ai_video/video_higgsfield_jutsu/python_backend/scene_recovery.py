"""Local, audited recovery of imperfect scene data without another model call."""
from copy import deepcopy
from .contract_validation import contract_issues
from .scene_direction import DIRECTION_SCHEMA, direction_candidate, compile_direction
from .scene_contract import execution_scene, SceneValidationError
from .scene_intent import prepare_authored_direction
from .common_scene_adapter import CONVENTIONS, _named_axes
from .canonical_scene import normalize_scene


def recover_scene(raw, *, source, duration, titles=()):
    if not isinstance(raw, dict) or not isinstance(raw.get('subjects'), list) or not any(
            isinstance(s, dict)
            and any(isinstance(s.get(k), str) and s[k].strip() for k in ('id','label'))
            and (s.get('kind') in ('human','person','prop','object','quadruped','bird')
                 or isinstance(s.get('position'), (list,dict))) for s in raw['subjects']):
        raise ValueError('유효한 피사체 정보가 없어 장면 복구 불가')
    beats = raw.get('beats')
    if not isinstance(beats, list) or not any(isinstance(b, dict) and (
            isinstance(b.get('intent'), str) and b['intent'].strip()
            or isinstance(b.get('actions'), list) and any(isinstance(a, dict)
                and isinstance(a.get('subject'), str) and isinstance(a.get('action'), str)
                for a in b['actions'])) for b in beats):
        raise ValueError('원본 동작·장면 정보가 없어 무관한 대체 영상을 생성하지 않습니다.')
    changes, errors = [], contract_issues(raw, DIRECTION_SCHEMA)
    defaults = {'movement':'locked', 'framing':'full', 'transition':'continuous', 'scope':'all',
                'role':'performer', 'action':'idle', 'kind':'prop', 'shape':'box',
                'purpose':'performance', 'weight':1, 'elevation':10, 'size':[1,1,1],
                'position':[0,0,0], 'color':[.5,.5,.5], 'background':[.12,.14,.18]}

    def repair(value, schema, path='', key=''):
        kind = schema.get('type')
        if kind == 'object':
            row = value if isinstance(value, dict) else {}
            props = schema['properties']
            result = {k:repair(row.get(k), spec, path+'.'+k if path else k, k)
                      for k,spec in props.items() if k in row or k in schema.get('required', [])}
        elif kind == 'array' and isinstance(value, list) and schema.get('items',{}).get('type') == 'object':
            result = [repair(v, schema['items'], f'{path}[{i}]') for i,v in enumerate(value[:schema.get('maxItems',160)])
                      if isinstance(v, dict)]
        elif not contract_issues(value, schema):
            return deepcopy(value)
        else:
            types = kind if isinstance(kind, list) else [kind]
            if 'null' in types:
                result = None
            elif key in defaults:
                result = deepcopy(defaults[key])
            elif 'enum' in schema:
                result = schema['enum'][0]
            elif kind == 'array':
                result = []
            elif kind in ('number','integer'):
                result = schema.get('minimum',0)
            else:
                result = source[:500] if key in ('intent','description') else 'recovered'
        if result != value:
            changes.append({'path':path, 'before':deepcopy(value), 'after':deepcopy(result),
                            'reason':'local_execution_recovery',
                            **({'allowedValues':schema['enum'][:]} if 'enum' in schema else {})})
        return result

    candidate = deepcopy(raw)
    try:
        candidate, named_changes = _named_axes(candidate)
        changes.extend(named_changes)
        candidate, normalized_changes = normalize_scene(candidate)
        changes.extend(normalized_changes)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        errors.append({'path':'normalization', 'message':str(error)})
    candidate = repair(candidate, DIRECTION_SCHEMA)
    if not candidate.get('beats'):
        raise ValueError('유효한 동작·장면 구간이 없어 장면 복구 불가')
    candidate, adjustments = prepare_authored_direction(candidate, source)
    try:
        design = compile_direction(candidate,duration,titles,adjustments=adjustments)
    except SceneValidationError as error:
        errors.extend(error.issues)
        design, repairs = execution_scene(direction_candidate(candidate,duration),duration,request=source)
        changes.extend({'path':'execution','reason':r} for r in repairs)
    return {'version':1, 'raw':deepcopy(raw), 'normalized':candidate, 'direction':candidate,
            'execution':design, 'changes':changes, 'adjustments':adjustments,
            'conventions':deepcopy(CONVENTIONS), 'errors':errors}
