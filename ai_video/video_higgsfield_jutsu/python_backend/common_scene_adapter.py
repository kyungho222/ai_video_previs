"""The model-independent boundary between authored responses and executable scenes."""
from copy import deepcopy
from .canonical_scene import normalize_scene, CanonicalSceneError
from .scene_response_adapter import extract_scene_object
from .scene_direction import compile_direction
from .scene_contract import validate_scene
from .scene_intent import prepare_authored_direction
from .scene_correction import correct_scene
from .scene_relations import prepare_initial_contacts, reconcile_relations, derive_movement_limits, RelationError
from .scene_state import normalize_support_relations, resolve_scene_state
from .object_union import merge_requested_props

CONVENTIONS = {'positionAxes':['x','y','z'], 'sizeAxes':['width','depth','height'],
               'positionUnits':'metres', 'humanPositionAnchor':'feet',
               'propPositionAnchor':'base', 'gestureTargetAnchor':'separate_from_root'}


def _nullable_defaults(value, schema, changes, path=''):
    if isinstance(value,dict):
        for key,spec in schema.get('properties',{}).items():
            child=path+'.'+key if path else key
            kind=spec.get('type')
            if key not in value and key in schema.get('required',[]) and isinstance(kind,list) and 'null' in kind:
                value[key]=None
                changes.append({'path':child,'after':None,'reason':'nullable_field_unspecified'})
            elif key in value:
                _nullable_defaults(value[key],spec,changes,child)
    elif isinstance(value,list) and isinstance(schema.get('items'),dict):
        for index,item in enumerate(value):
            _nullable_defaults(item,schema['items'],changes,f'{path}[{index}]')


def _named_axes(value):
    result, changes = deepcopy(value), []
    def convert(row, field, names, path):
        if not isinstance(row, dict) or not isinstance(row.get(field), dict):
            return
        source=row[field]
        if set(source) != set(names):
            raise CanonicalSceneError(path+'.'+field+': 명시적 좌표 축이 누락되거나 중복됐습니다.')
        row[field]=[source[key] for key in names]
        changes.append({'path':path+'.'+field,'before':deepcopy(source),'after':row[field][:],
                        'reason':'explicit_named_axes'})
    if isinstance(result,dict):
        for i,subject in enumerate(result.get('subjects',[]) if isinstance(result.get('subjects'),list) else []):
            convert(subject,'size',('width','depth','height'),f'subjects[{i}]')
            convert(subject,'position',('x','y','z'),f'subjects[{i}]')
        for i,beat in enumerate(result.get('beats',[]) if isinstance(result.get('beats'),list) else []):
            for j,action in enumerate(beat.get('actions',[]) if isinstance(beat,dict) and isinstance(beat.get('actions'),list) else []):
                convert(action,'position',('x','y','z'),f'beats[{i}].actions[{j}]')
    return result, changes


def adapt_scene_response(response, *, request, duration, titles=()):
    raw=deepcopy(extract_scene_object(response))
    changes, normalized_changes, adjustments = [], [], []
    normalized = direction = None
    try:
        named, changes=_named_axes(raw)
        from .scene_direction import DIRECTION_SCHEMA
        _nullable_defaults(named,DIRECTION_SCHEMA,changes)
        normalized, normalized_changes=normalize_scene(named)
        direction, adjustments=prepare_authored_direction(normalized, request)
        execution=compile_direction(direction,duration,titles,adjustments=adjustments)
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        error.scene_adapter_trace = {'version':1, 'raw':raw, 'normalized':normalized,
                                     'direction':direction,'changes':changes+normalized_changes,
                                     'adjustments':adjustments}
        raise
    return {'version':1, 'raw':raw, 'normalized':normalized, 'direction':direction,
            'execution':execution, 'changes':changes+normalized_changes,
            'adjustments':adjustments, 'conventions':deepcopy(CONVENTIONS)}


def prepare_scene_execution(design, *, relations, request, limits=None):
    execution, objects=merge_requested_props(request,design)
    if objects['skipped']:
        raise RelationError('필수 객체 수가 씬 한도를 초과합니다: '+str(objects['skipped']))
    before=deepcopy(execution)
    execution, correction=correct_scene(execution)
    execution, relations, contacts=prepare_initial_contacts(execution,relations,request)
    correction['initialContact']=contacts
    supports, spatial, support_changes=normalize_support_relations(execution,relations,request)
    subjects={s['id']:s for s in execution['subjects']}
    unresolved, usable_supports=[],[]
    generated={row['id'] for row in objects['added']}
    for relation in supports:
        target=subjects.get(relation.get('target'))
        size=target.get('size') if target else None
        if (target and (target['id'] in generated or
                (isinstance(size,list) and len(size)==3 and size[2]>2*min(size[:2])))):
            unresolved.append({'stage':'support_geometry','subject':relation.get('subject'),
                               'target':target['id'],'size':deepcopy(size),
                               'reason':'support_axes_or_placeholder_geometry_unconfirmed'})
        else:
            usable_supports.append(relation)
    execution, relation_quality=reconcile_relations(execution,spatial,
        limits=derive_movement_limits(request,execution) if limits is None else limits)
    execution, state=resolve_scene_state(execution,usable_supports)
    state['relationNormalization']=support_changes
    state['warnings'].extend(unresolved)
    state['unresolvedSupports']=[deepcopy(r) for r in supports if r not in usable_supports]
    return {'version':1,'execution':execution,'beforeCorrection':before,'objects':objects,
            'correction':correction,'relationQuality':relation_quality,'sceneState':state,
            'unresolved':deepcopy(state['warnings']),
            'conventions':deepcopy(CONVENTIONS)}
