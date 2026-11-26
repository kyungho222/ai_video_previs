"""Data-only scene direction. No model-provided Python or remote tool arguments."""
import copy
import math
import re
from .contract_validation import contract_issues

HUMAN_GESTURES = ('salute', 'handshake', 'push', 'pull', 'arms_up', 'kick_left', 'stomp_left', 'step_side', 'step_back', 'foot_tap')
ACTIONS = HUMAN_GESTURES + ('idle', 'speak', 'walk', 'run', 'wave', 'wave_both', 'hand_heart', 'clap', 'kick', 'stomp', 'dance', 'raise_hand', 'point', 'reach', 'hold',
           'slap', 'turn', 'crouch', 'sit', 'jump', 'fall', 'lie_down', 'fly', 'move')
KINDS = ('human', 'quadruped', 'bird', 'prop')
FRAMINGS = ('wide', 'full', 'medium', 'close')
MOVEMENTS = ('locked', 'track', 'orbit', 'push', 'pan')


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def array(items):
    return {'type': 'array', 'items': items}


TEXT = {'type': 'string'}
NUMBER = {'type': 'number'}
CAMERA_ELEVATION = {**NUMBER, 'minimum': -45, 'maximum': 70}
VECTOR = {**array(NUMBER), 'minItems': 3, 'maxItems': 3}
CAMERA = obj({'focus': array(TEXT), 'framing': {'type': 'string', 'enum': list(FRAMINGS)},
              'movement': {'type': 'string', 'enum': list(MOVEMENTS)},
              'azimuth': NUMBER, 'end_azimuth': NUMBER, 'elevation': CAMERA_ELEVATION})
# Optional for archived scenes; strict authored directions require transition.
CAMERA['properties']['transition'] = {'type': 'string', 'enum': ['cut', 'continuous']}
SCENE_SCHEMA = obj({
    'description': TEXT,
    'subjects': array(obj({'id': TEXT, 'label': TEXT, 'kind': {'type': 'string', 'enum': list(KINDS)},
                           'shape': {'type': 'string', 'enum': ['box', 'sphere', 'cylinder']},
                           'color': VECTOR, 'size': VECTOR, 'position': VECTOR, 'yaw': NUMBER})),
    'tracks': array(obj({'subject': TEXT, 'action': {'type': 'string', 'enum': list(ACTIONS)},
                         'start': NUMBER, 'end': NUMBER, 'position': VECTOR, 'yaw': NUMBER,
                         'target': {'type': ['string', 'null']}})),
    'scenes': array(obj({'title': TEXT, 'description': TEXT, 'start': NUMBER, 'end': NUMBER,
                         'camera': CAMERA})),
    'background': VECTOR,
})
SCENE_SCHEMA['properties']['subjects'].update(minItems=1, maxItems=24)
SCENE_SCHEMA['properties']['tracks'].update(maxItems=160)
SCENE_SCHEMA['properties']['scenes'].update(minItems=1, maxItems=40)
CAMERA['properties']['focus'].update(minItems=1, maxItems=24)
# Optional for legacy tracks; new authored directions receive deterministic channels.
SCENE_SCHEMA['properties']['tracks']['items']['properties']['channel'] = {'type': 'string', 'enum': ['movement', 'posture', 'gesture', 'vocal']}
SCENE_SCHEMA['properties']['tracks']['items']['properties']['initialPosture'] = {'type': 'boolean'}


def action_channel(action):
    if action == 'speak':
        return 'vocal'
    if action in ('sit', 'crouch', 'lie_down'):
        return 'posture'
    if action in ('wave', 'wave_both', 'hand_heart', 'clap', 'raise_hand', 'point', 'reach', 'hold', 'slap', 'salute', 'handshake', 'push', 'pull', 'arms_up'):
        return 'gesture'
    return 'movement'


for _name in ('tracks', 'scenes'):
    SCENE_SCHEMA['properties'][_name]['items']['properties']['sourceBeat'] = {'type': 'integer', 'minimum': 0}


class SceneValidationError(ValueError):
    """All independently detectable issues from one unchanged scene candidate."""

    def __init__(self, issues):
        self.issues = issues
        super().__init__('Scene validation failed:\n' + '\n'.join(
            f"{index}. {issue['path']}: {issue['message']}" for index, issue in enumerate(issues, 1)))


def validate_scene(value, duration, scene_count=None):
    """Collect structural and semantic errors before requesting a model revision."""
    issues = []

    def fail(path, message):
        issue = {'path': path, 'message': message}
        if issue not in issues:
            issues.append(issue)

    def finite(v):
        return type(v) in (int, float) and math.isfinite(v)

    def number(v, low, high, path):
        if finite(v) and not low <= v <= high:
            fail(path, 'finite number in ' + str((low, high)))

    def vector(v, low, high, path):
        if isinstance(v, list):
            for i, item in enumerate(v):
                number(item, low, high, f'{path}[{i}]')

    def text(v, maximum, path):
        if isinstance(v, str) and (not v.strip() or len(v) > maximum):
            fail(path, 'nonempty bounded text required')

    def rows(key):
        raw = value.get(key)
        return enumerate(raw) if isinstance(raw, list) else []

    issues.extend(contract_issues(value, SCENE_SCHEMA))
    if not isinstance(value, dict):
        raise SceneValidationError(issues)
    text(value.get('description'), 8000, 'description')
    vector(value.get('background'), 0, 1, 'background')
    ids, kinds = set(), {}
    for i, s in rows('subjects'):
        if not isinstance(s, dict):
            continue
        path = f'subjects[{i}]'
        ident = s.get('id')
        if isinstance(ident, str):
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', ident) or ident in ids:
                fail(path + '.id', 'subject id must be unique ASCII identifier')
            ids.add(ident)
            kinds[ident] = s.get('kind')
        text(s.get('label'), 120, path + '.label')
        for key, low, high in (('color', 0, 1), ('size', .05, 20), ('position', -50, 50)):
            vector(s.get(key), low, high, path + '.' + key)
        pos = s.get('position')
        if isinstance(pos, list) and len(pos) > 2 and finite(pos[2]) and pos[2] < 0:
            fail(path + '.position[2]', 'subject position.z must be at or above ground')
        number(s.get('yaw'), -720, 720, path + '.yaw')
    previous = {}
    human_actions = HUMAN_GESTURES + ('slap', 'point', 'raise_hand', 'wave', 'wave_both', 'hand_heart', 'clap', 'kick', 'stomp', 'dance', 'sit', 'speak')
    for i, t in rows('tracks'):
        if not isinstance(t, dict):
            continue
        path = f'tracks[{i}]'
        actor, target = t.get('subject'), t.get('target')
        known_actor = isinstance(actor, str) and actor in ids
        if isinstance(actor, str) and not known_actor:
            fail(path + '.subject', 'unknown subject: ' + actor)
        if isinstance(target, str) and target not in ids and target != 'camera':
            fail(path + '.target', 'unknown target: ' + target)
        if isinstance(actor, str) and target == actor:
            fail(path + '.target', 'action target must be a different entity')
        if known_actor and kinds[actor] in KINDS and kinds[actor] != 'human' and t.get('action') in human_actions:
            fail(path + '.action', 'this articulated action requires a human proxy')
        start, end = t.get('start'), t.get('end')
        number(start, 0, duration, path + '.start')
        number(end, 0, duration, path + '.end')
        if finite(start) and finite(end):
            key = (actor, t.get('channel', 'legacy'))
            if end - start < 1 / 24 or (known_actor and start < previous.get(key, 0) - 1e-6):
                fail(path, 'actor tracks must be ordered, nonoverlapping and last at least one frame')
            if known_actor:
                previous[key] = end
        vector(t.get('position'), -50, 50, path + '.position')
        pos = t.get('position')
        if isinstance(pos, list) and len(pos) > 2 and finite(pos[2]) and pos[2] < 0:
            fail(path + '.position[2]', 'track.position.z must be at or above ground')
        number(t.get('yaw'), -720, 720, path + '.yaw')
    scenes = value.get('scenes')
    if scene_count and isinstance(scenes, list) and len(scenes) != scene_count:
        fail('scenes', 'scenes must match scenario card count: ' + str(scene_count))
    last = 0
    for i, s in rows('scenes'):
        if not isinstance(s, dict):
            last = None
            continue
        path = f'scenes[{i}]'
        text(s.get('title'), 120, path + '.title')
        text(s.get('description'), 4000, path + '.description')
        start, end = s.get('start'), s.get('end')
        number(start, 0, duration, path + '.start')
        number(end, 0, duration, path + '.end')
        if finite(start) and finite(end) and ((last is not None and abs(start - last) > .001) or end - start < 1 / 24):
            fail(path, 'scene intervals must continuously cover the duration')
        last = end if finite(end) else None
        c = s.get('camera')
        if not isinstance(c, dict):
            continue
        focus = c.get('focus')
        if isinstance(focus, list):
            valid_refs = all(isinstance(v, str) and v in ids for v in focus)
            if not focus or not valid_refs or (valid_refs and len(set(focus)) != len(focus)):
                fail(path + '.camera.focus', 'camera.focus must reference existing unique entities')
        for key in ('azimuth', 'end_azimuth'):
            number(c.get(key), -720, 720, path + '.camera.' + key)
    if last is not None and abs(last - duration) > .001:
        fail('scenes', 'scenes must end at requested duration')
    if issues:
        raise SceneValidationError(issues)
    return copy.deepcopy(value)


def execution_scene(value, duration, *, titles=(), request=''):
    """After the repair budget, retain the final data with bounded execution defaults.

    This is a local data repair, never another AI call or model-code execution.
    The caller keeps the original candidate and validation errors for inspection.
    """
    source = value if isinstance(value, dict) else {}
    repairs = []

    def text(value, default, limit):
        return value.strip()[:limit] if isinstance(value, str) and value.strip() else default[:limit]

    def number(value, low, high, default=0):
        return min(high, max(low, value)) if type(value) in (int, float) and math.isfinite(value) else default

    def vector(value, low, high, default, *, ground=False):
        items = value if isinstance(value, list) else []
        result = [number(items[i] if i < len(items) else None, low, high, default[i]) for i in range(3)]
        if ground:
            result[2] = max(0, result[2])
        return result

    subjects, aliases = [], {}
    supplied = source.get('subjects')
    supplied = [s for s in supplied if isinstance(s, dict)][:24] if isinstance(supplied, list) else []
    if not supplied:
        supplied = [{}]
        repairs.append('대상 정보 누락: 기본 프리비즈 대상 1개 구성')
    for index, subject in enumerate(supplied):
        raw_id = subject.get('id')
        ident = raw_id if isinstance(raw_id, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', raw_id) else f'subject_{index + 1}'
        known = {s['id'] for s in subjects}
        if ident in known:
            ident = f'subject_{index + 1}'
        suffix = 1
        while ident in known:
            ident = f'subject_{index + 1}_{suffix}'
            suffix += 1
        if isinstance(raw_id, str):
            aliases.setdefault(raw_id, ident)
        subjects.append({'id': ident, 'label': text(subject.get('label'), f'대상 {index + 1}', 120),
            'kind': subject.get('kind') if subject.get('kind') in KINDS else 'human',
            'shape': subject.get('shape') if subject.get('shape') in ('box', 'sphere', 'cylinder') else 'box',
            'color': vector(subject.get('color'), 0, 1, [.4, .5, .7]),
            'size': vector(subject.get('size'), .05, 20, [1, 1, 1]),
            'position': vector(subject.get('position'), -50, 50, [0, 0, 0], ground=True),
            'yaw': number(subject.get('yaw'), -720, 720)})
    ids = {s['id'] for s in subjects}
    kinds = {s['id']: s['kind'] for s in subjects}

    def reference(raw):
        return aliases.get(raw, raw) if isinstance(raw, str) else None

    tracks = source.get('tracks')
    tracks = [t for t in tracks if isinstance(t, dict)][:160] if isinstance(tracks, list) else []
    previous, normalized_tracks = {}, []
    for track in sorted(tracks, key=lambda t: number(t.get('start'), 0, duration)):
        subject = reference(track.get('subject'))
        if subject not in ids:
            repairs.append('알 수 없는 대상의 동작 제외')
            continue
        start = max(previous.get(subject, 0), number(track.get('start'), 0, duration))
        minimum_span = 1 / 24 + 1e-6
        if duration - start < minimum_span:
            repairs.append('출력 길이 밖의 동작 제외')
            continue
        end = number(track.get('end'), start + minimum_span, duration, duration)
        target = reference(track.get('target'))
        target = target if target in ids and target != subject else None
        action = track.get('action') if track.get('action') in ACTIONS else 'move'
        if kinds[subject] != 'human' and action in HUMAN_GESTURES + ('slap', 'point', 'raise_hand', 'wave', 'wave_both', 'hand_heart', 'clap', 'kick', 'stomp', 'dance', 'sit', 'speak'):
            action = 'move'
        normalized_tracks.append({'subject': subject, 'action': action, 'start': start, 'end': end,
            'position': vector(track.get('position'), -50, 50, [0, 0, 0], ground=True),
            'yaw': number(track.get('yaw'), -720, 720), 'target': target})
        previous[subject] = end
    raw_scenes = source.get('scenes')
    raw_scenes = [s for s in raw_scenes if isinstance(s, dict)][:40] if isinstance(raw_scenes, list) else []
    count = len(titles) or len(raw_scenes) or 1
    valid_times = len(raw_scenes) == count
    last = 0
    for s in raw_scenes:
        start, end = s.get('start'), s.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start <= duration or not 0 <= end <= duration
                or abs(start-last) > .001 or end-start < 1/24 or end > duration):
            valid_times = False
            break
        last = end
    valid_times = valid_times and abs(last-duration) <= .001
    description = text(source.get('description'), request or '프리비즈 장면', 8000)
    scenes = []
    for index in range(count):
        interval = raw_scenes[min(index, len(raw_scenes)-1)] if raw_scenes else {}
        camera = interval.get('camera')
        camera = camera if isinstance(camera, dict) else {}
        raw_focus = camera.get('focus')
        focus = list(dict.fromkeys(reference(v) for v in raw_focus if reference(v) in ids)) if isinstance(raw_focus, list) else []
        scenes.append({'title': titles[index] if titles else text(interval.get('title'), f'장면 {index + 1}', 120),
            'description': text(interval.get('description'), description, 4000),
            'start': interval['start'] if valid_times else duration*index/count,
            'end': interval['end'] if valid_times else duration*(index+1)/count,
            **({'sourceBeat': interval['sourceBeat']} if type(interval.get('sourceBeat')) is int and interval['sourceBeat'] >= 0 else {}),
            'camera': {'transition': camera.get('transition') if camera.get('transition') in ('cut', 'continuous') else 'continuous', 'focus': focus or [s['id'] for s in subjects],
                'framing': camera.get('framing') if camera.get('framing') in FRAMINGS else 'full',
                'movement': camera.get('movement') if camera.get('movement') in MOVEMENTS else 'track',
                'azimuth': number(camera.get('azimuth'), -720, 720),
                'end_azimuth': number(camera.get('end_azimuth'), -720, 720),
                'elevation': number(camera.get('elevation'), CAMERA_ELEVATION['minimum'], CAMERA_ELEVATION['maximum'], 10)}})
    design = {'description': description, 'subjects': subjects, 'tracks': normalized_tracks, 'scenes': scenes,
              'background': vector(source.get('background'), 0, 1, [.12, .14, .18])}
    if set(source) - set(design):
        repairs.append('지원하지 않는 장면 필드 제외')
    for key in design:
        if design[key] != source.get(key):
            repairs.append(key + ': 실행 가능한 필수값·범위로 보정')
    return validate_scene(design, duration, len(titles) or None), repairs


def legacy_plan(scene):
    """Keep existing snapshot and video-generation consumers independent of this refactor."""
    timeline = [{'start': s['start'], 'end': s['end'], 'subject_action': s['description'],
                 'camera_goal': s['camera']['framing'] + ' / ' + s['camera']['movement']}
                for s in scene['scenes']]
    camera = '; '.join(s['camera_goal'] for s in timeline)
    return {'scene_action': scene['description'], 'camera': camera, 'motion_detail': 'basic_motion',
            'timeline': timeline, 'preprocessed_prompt': {
                'scene': [scene['description']], 'choreography': [s['description'] for s in scene['scenes']],
                'motion': [t['subject'] + ': ' + t['action'] for t in scene['tracks']] or ['정지'],
                'camera': [camera], 'mood_style': ['편집 가능한 3D 프리비즈']}}
