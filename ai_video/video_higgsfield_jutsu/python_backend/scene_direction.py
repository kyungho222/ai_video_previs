"""One authored performance sequence, compiled to action and camera together."""
from copy import deepcopy
import math
import re
from .contract_validation import contract_issues

from .scene_contract import SCENE_SCHEMA, CAMERA, SceneValidationError, obj, array, validate_scene, action_channel
from .resources.scene_props import is_display_prop


_action = deepcopy(SCENE_SCHEMA['properties']['tracks']['items'])
_action['properties'].pop('sourceBeat', None)
_action['properties'].pop('channel', None)
_action['properties'].pop('initialPosture', None)
for _key in ('start', 'end'):
    del _action['properties'][_key]
    _action['required'].remove(_key)
_action['properties']['role'] = {'type': 'string', 'enum': ['performer', 'speaker', 'observer']}
_action['required'].append('role')
_camera = deepcopy(CAMERA)
del _camera['properties']['focus']
_camera['properties']['scope'] = {'type': 'string', 'enum': ['performers', 'all']}
_camera['required'] = [k for k in _camera['properties'] if k != 'transition']
_camera['properties']['directionEvidence'] = {'type': ['string', 'null']}
DIRECTION_SCHEMA = obj({
    'description': {'type': 'string'},
    'subjects': deepcopy(SCENE_SCHEMA['properties']['subjects']),
    'background': deepcopy(SCENE_SCHEMA['properties']['background']),
    'beats': {**array(obj({
        'card': {'type': 'integer', 'minimum': 0},
        'purpose': {'type': 'string', 'enum': ['performance', 'introduction', 'wait']},
        'intent': {'type': 'string'},
        'weight': {'type': 'number', 'minimum': .001, 'maximum': 100},
        'seconds': {'type': ['number', 'null'], 'minimum': 1 / 24},
        'actions': {**array(_action), 'minItems': 1, 'maxItems': 24},
        'camera': _camera,
    })), 'minItems': 1, 'maxItems': 40},
})


# Separate relative spatial relationships from subject geometry and animation.
DIRECTION_SCHEMA['properties']['relations'] = {**array(obj({
    'subject': {'type': 'string'}, 'target': {'type': 'string'},
    'kind': {'type': 'string', 'enum': ['left_of', 'right_of', 'behind', 'in_front_of', 'supported_by', 'unsupported']},
    'scope': {'type': 'string', 'enum': ['initial', 'entire']},
    'evidence': {'type': 'string'},
})), 'maxItems': 48}

# Optional in legacy snapshots, required in strict new responses.
DIRECTION_SCHEMA['properties']['relations']['items']['properties']['referenceFrame'] = {
    'type': 'string', 'enum': ['screen', 'world', 'object']}
DIRECTION_SCHEMA['properties']['requirements'] = array(obj({
    'id': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['camera', 'action', 'setting', 'appearance', 'dialogue']},
    'sourceQuote': {'type': 'string'}, 'beats': array({'type': 'integer', 'minimum': 0}),
}))
DIRECTION_SCHEMA['properties']['requirements']['items']['properties'].update({
    'value': {'type': ['string', 'null']}, 'subjects': array({'type': 'string'}),
})
DIRECTION_SCHEMA['properties']['beats']['items']['properties']['timingEvidence'] = {'type': ['string', 'null']}


def direction_wire_schema():
    result = deepcopy(DIRECTION_SCHEMA)
    def require_all(schema):
        if schema.get('type') == 'object':
            schema['required'] = list(schema['properties'])
            for child in schema['properties'].values():
                require_all(child)
        elif schema.get('type') == 'array':
            require_all(schema['items'])
    require_all(result)
    return result


def sequential_root_actors(beat):
    intent = beat.get('intent', '')
    if not isinstance(intent, str) or re.search(r'simultaneously|at the same time|동시에', intent, re.I):
        return set()
    if not re.search(r'\b(?:then|after|before)\b|and\s+(?:leans?|crouches?|sits?|kneels?|bends?)\b|이후|고 나서|나온 뒤|한 뒤', intent, re.I):
        return set()
    counts = {}
    for action in beat.get('actions', []):
        if isinstance(action, dict) and action_channel(action.get('action')) in ('movement','posture'):
            actor = action.get('subject')
            if isinstance(actor, str):
                counts[actor] = counts.get(actor, 0) + 1
    return {actor for actor, count in counts.items() if count > 1}


def compile_direction(value, duration, titles=(), *, adjustments=None):
    """Validate one direction; derive synchronous intervals and focus in Python.

    Explicit seconds are reserved first. Remaining beats share the remaining
    duration by weight. There is no automatically inserted preparation interval.
    """
    issues = []

    def fail(path, message):
        issues.append({'path': path, 'message': message})

    def finite(v):
        return type(v) in (int, float) and math.isfinite(v)

    issues.extend(contract_issues(value, DIRECTION_SCHEMA))
    if not isinstance(value, dict):
        raise SceneValidationError(issues)
    subjects = value.get('subjects') if isinstance(value.get('subjects'), list) else []
    ids = {s['id'] for s in subjects if isinstance(s, dict) and isinstance(s.get('id'), str)}
    beats = value.get('beats') if isinstance(value.get('beats'), list) else []
    cards = []
    for index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            continue
        path = f'beats[{index}]'
        card = beat.get('card')
        if type(card) is int:
            cards.append(card)
        if not isinstance(beat.get('intent'), str) or not beat['intent'].strip():
            fail(path + '.intent', 'nonempty intent required')
        actors = set()
        actor_roots = {}
        sequential = sequential_root_actors(beat)
        actions = beat.get('actions') if isinstance(beat.get('actions'), list) else []
        for row, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            p = f'{path}.actions[{row}]'
            actor, target = action.get('subject'), action.get('target')
            if isinstance(actor, str):
                if actor not in ids:
                    fail(p + '.subject', 'unknown subject: ' + actor)
                channel = action_channel(action.get('action'))
                root = (action.get('position'), action.get('yaw'))
                ordered_root = actor in sequential and channel in ('movement', 'posture')
                if actor in actor_roots and root != actor_roots[actor] and not ordered_root:
                    fail(p + '.position', 'simultaneous channels require the same root position and yaw')
                else:
                    actor_roots[actor] = root
                if (actor, channel) in actors and not ordered_root:
                    fail(p + '.subject', 'overlapping actions in the same channel are unsupported')
                if not ordered_root and channel in ('movement', 'posture') and any((actor, x) in actors for x in ('movement', 'posture')):
                    fail(p + '.subject', 'simultaneous movement and posture changes are unsupported')
                actors.add((actor, channel))
            if isinstance(target, str) and target not in ids and target != 'camera':
                fail(p + '.target', 'unknown target: ' + target)
    if titles and (cards != sorted(cards) or set(cards) != set(range(len(titles)))):
        fail('beats', 'cover every scenario card in order; multiple beats per card are allowed')
    if not finite(duration) or duration <= 0:
        fail('duration', 'positive finite duration required')
    if issues:
        raise SceneValidationError(issues)

    original_count = len(beats)
    if adjustments is not None:
        for i, beat in enumerate(beats):
            for actor in sorted(sequential_root_actors(beat)):
                adjustments.append({'beat': i, 'subject': actor, 'reason': 'sequential_root_actions'})
    value = prepare_direction(value, adjustments)
    beats = value['beats']
    removed_count = original_count - len(beats)
    fixed = sum(b['seconds'] for b in beats if b['seconds'] is not None)
    flexible = [b for b in beats if b['seconds'] is None]
    remaining = duration - fixed
    if remaining < -1e-6 or (not flexible and abs(remaining) > 1e-6):
        raise SceneValidationError([{'path': 'beats', 'message': 'explicit durations must fit the requested duration; use seconds=null for flexible beats'}])
    weight = sum(b['weight'] for b in flexible)
    spans = [b['seconds'] if b['seconds'] is not None else remaining * b['weight'] / weight for b in beats]
    if any(span < 1 / 24 - 1e-9 for span in spans):
        raise SceneValidationError([{'path': 'beats', 'message': 'every performance needs at least one frame within the requested duration'}])
    scene = direction_candidate(value, duration, titles, spans=spans)
    try:
        return validate_scene(scene, duration)
    except SceneValidationError as error:
        origins = [f'beats[{i + removed_count}].actions[{j}]' for i, b in enumerate(beats) for j, a in enumerate(b['actions'])]
        remapped = []
        for issue in error.issues:
            path = re.sub(r'^tracks\[(\d+)\]', lambda m: origins[int(m[1])], issue['path'])
            def origin(match):
                index = int(match[1])
                if removed_count and len(scene['scenes']) > len(beats):
                    index = 0 if index == 0 else index - 1 + removed_count
                else:
                    index += removed_count
                return f'beats[{index}]'
            path = re.sub(r'^scenes\[(\d+)\]', origin, path)
            remapped.append({**issue, 'path': path})
        raise SceneValidationError(remapped) from error


def prepare_direction(value, adjustments=None):
    """Remove untimed static introductions; explicit waits retain their interval."""
    result = deepcopy(value)
    beats = result.get('beats')
    if not isinstance(beats, list):
        return result
    for index, beat in enumerate(beats):
        if not isinstance(beat, dict) or not isinstance(beat.get('actions'), list):
            continue
        if beat.get('purpose') == 'wait' or (beat.get('purpose') == 'introduction' and beat.get('seconds') is not None):
            continue
        for action in beat['actions']:
            if isinstance(action, dict) and action.get('role') == 'speaker' and action.get('action') == 'idle':
                action['action'] = 'speak'
                report = {'reason': 'speaker_idle_to_speak', 'beat': index, 'subject': action.get('subject')}
                if adjustments is not None and report not in adjustments:
                    adjustments.append(report)
    introductions = []
    while len(beats) > 1:
        first = beats[0]
        if not isinstance(first, dict) or first.get('purpose') != 'introduction' or first.get('seconds') is not None:
            break
        actions = first.get('actions')
        if not isinstance(actions, list) or not actions or not all(isinstance(a, dict) and a.get('action') == 'idle' for a in actions):
            break
        removed = beats.pop(0)
        introductions.append(removed)
        report = {'reason': 'remove_untimed_idle_introduction', 'card': removed.get('card'), 'intent': removed.get('intent')}
        if adjustments is not None and report not in adjustments:
            adjustments.append(report)
    if introductions:
        result['_introductions'] = introductions
    return result


def direction_candidate(value, duration, titles=(), *, spans=None, adjustments=None):
    """Project beats before existing bounded completion repair, keeping useful data.

    Normal planning supplies validated spans. The exhausted-budget path projects
    partial model data so execution_scene can apply its existing completion policy
    without throwing away every performance merely because the contract changed.
    """
    source = value if isinstance(value, dict) else {}
    if spans is None:
        source = prepare_direction(source, adjustments)
    subjects = source.get('subjects') if isinstance(source.get('subjects'), list) else []
    beats = source.get('beats') if isinstance(source.get('beats'), list) else []
    beats = [b for b in beats if isinstance(b, dict)][:40]
    if spans is None:
        def positive(v):
            return type(v) in (int, float) and math.isfinite(v) and v > 0
        seconds = [b.get('seconds') if positive(b.get('seconds')) else None for b in beats]
        weights = [b.get('weight') if positive(b.get('weight')) else 1 for b in beats]
        fixed = sum(s for s in seconds if s is not None)
        flexible_weight = sum(w for s, w in zip(seconds, weights) if s is None)
        if flexible_weight and duration - fixed >= sum(s is None for s in seconds) / 24:
            spans = [s if s is not None else (duration - fixed) * w / flexible_weight
                     for s, w in zip(seconds, weights)]
        elif not flexible_weight and abs(duration - fixed) <= 1e-6:
            spans = seconds
        else:
            # Only impossible timing needs the existing bounded completion policy.
            weights = [s if s is not None else w for s, w in zip(seconds, weights)]
            total = sum(weights)
            spans = [duration * w / total for w in weights]
    tracks, scenes = [], []
    cast = [s['id'] for s in subjects if isinstance(s, dict) and isinstance(s.get('id'), str) and s.get('kind') != 'prop']
    object_ids = {s['id'] for s in subjects if isinstance(s, dict) and isinstance(s.get('id'), str)}
    displays = {s['id'] for s in subjects if isinstance(s, dict) and s.get('kind') == 'prop' and is_display_prop(s.get('label'))}
    cursor = 0
    for index, (beat, span) in enumerate(zip(beats, spans)):
        source_index = index + len(source.get('_introductions', []))
        end = duration if index == len(beats) - 1 else cursor + span
        actions = beat.get('actions') if isinstance(beat.get('actions'), list) else []
        actions = [a for a in actions if isinstance(a, dict)]
        primary = [a for a in actions if a.get('role') in ('performer', 'speaker') or
                   (a.get('role') is None and a.get('action') != 'idle')]
        performers = [a['subject'] for a in primary if isinstance(a.get('subject'), str)]
        # Seats are supports, but the visible object being pointed at is part
        # of the shot's meaning. Camera/world reference tokens are not objects.
        partners = [a['target'] for a in primary if isinstance(a.get('target'), str)
                    and a['target'] in object_ids and (a['target'] in cast or a.get('action') == 'point')]
        raw_camera = beat.get('camera') if isinstance(beat.get('camera'), dict) else {}
        if raw_camera.get('framing') != 'close':
            for relation in source.get('relations', []):
                if (not isinstance(relation, dict) or relation.get('referenceFrame', 'screen') != 'screen'
                        or relation.get('kind') not in ('left_of', 'right_of', 'behind', 'in_front_of')
                        or (relation.get('scope') == 'initial' and index != 0)):
                    continue
                a, b = relation.get('subject'), relation.get('target')
                if a in performers and b in displays:
                    partners.append(b)
                elif b in performers and a in displays:
                    partners.append(a)
        focus = list(dict.fromkeys((cast if raw_camera.get('scope') == 'all' else []) + performers + partners))
        if not focus:
            # A stationary/wait beat still needs a camera; never invent a speaker.
            focus = cast or [s['id'] for s in subjects if isinstance(s, dict) and isinstance(s.get('id'), str)]
        sequential = sequential_root_actors(beat)
        root_counts = {actor: sum(a.get('subject') == actor and action_channel(a.get('action')) in ('movement', 'posture') for a in actions)
                       for actor in sequential}
        root_indices = dict.fromkeys(sequential, 0)
        for action in actions:
            start, finish = cursor, end
            actor = action.get('subject')
            if actor in sequential and action_channel(action.get('action')) in ('movement', 'posture'):
                slot = root_indices[actor]
                start, finish = cursor + span * slot / root_counts[actor], cursor + span * (slot + 1) / root_counts[actor]
                root_indices[actor] += 1
            tracks.append({**{k: deepcopy(v) for k, v in action.items() if k in SCENE_SCHEMA['properties']['tracks']['items']['properties']},
                           'start': start, 'end': finish, 'sourceBeat': source_index, 'channel': action_channel(action.get('action'))})
        camera = {k: deepcopy(v) for k, v in raw_camera.items() if k in CAMERA['properties'] and k != 'focus'}
        camera['focus'] = focus
        card = beat.get('card')
        title = titles[card] if type(card) is int and 0 <= card < len(titles) else f'구간 {index + 1}'
        description = beat.get('intent')
        shot_start = cursor
        introductions = source.get('_introductions', []) if index == 0 else []
        if introductions:
            intro = introductions[0]
            intro_setup = intro.get('camera') if isinstance(intro.get('camera'), dict) else {}
            intro_camera = {k: deepcopy(v) for k, v in intro_setup.items() if k in CAMERA['properties'] and k != 'focus'}
            intro_ids = [a['subject'] for a in intro.get('actions', []) if isinstance(a, dict) and isinstance(a.get('subject'), str)]
            intro_camera['focus'] = cast if intro_setup.get('scope') == 'all' else list(dict.fromkeys(intro_ids)) or cast
            intro_camera['focus'] = list(dict.fromkeys(intro_camera['focus'] + performers + partners))
            intro_titles = [titles[b['card']] for b in introductions if type(b.get('card')) is int and 0 <= b['card'] < len(titles)]
            intro_title = ' / '.join(dict.fromkeys(intro_titles))[:120] or '도입'
            intro_description = '\n'.join(str(b.get('intent') or '') for b in introductions)
            intro_description = (intro_description + '\n' + str(description or ''))[:4000]
            if span >= 2 / 24:
                shot_start = cursor + min(.5, max(1 / 24, span * .25))
                scenes.append({'title': intro_title, 'description': intro_description,
                               'start': cursor, 'end': shot_start, 'camera': intro_camera, 'sourceBeat': 0})
            else:
                # A one-frame action cannot hold two shots: merge both cards.
                title = (intro_title + ' / ' + title)[:120]
                description, camera = intro_description, intro_camera
        scenes.append({'title': title, 'description': description, 'start': shot_start, 'end': end, 'camera': camera, 'sourceBeat': source_index})
        cursor = end
    return {'description': source.get('description'), 'subjects': deepcopy(subjects),
            'background': deepcopy(source.get('background')), 'tracks': tracks, 'scenes': scenes}
