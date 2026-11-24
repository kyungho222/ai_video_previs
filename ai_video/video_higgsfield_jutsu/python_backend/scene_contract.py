"""Data-only scene direction. No model-provided Python or remote tool arguments."""
import copy
import math
import re

ACTIONS = ('idle', 'walk', 'run', 'wave', 'raise_hand', 'point', 'reach', 'hold',
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
CAMERA_ELEVATION = {**NUMBER, 'minimum': -10, 'maximum': 70}
VECTOR = {**array(NUMBER), 'minItems': 3, 'maxItems': 3}
CAMERA = obj({'focus': array(TEXT), 'framing': {'type': 'string', 'enum': list(FRAMINGS)},
              'movement': {'type': 'string', 'enum': list(MOVEMENTS)},
              'azimuth': NUMBER, 'end_azimuth': NUMBER, 'elevation': CAMERA_ELEVATION})
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


def validate_scene(value, duration, scene_count=None):
    """Validate bounded execution data, including references and disjoint actor tracks."""
    def fail(message):
        raise ValueError(message)

    def fields(value, schema, label):
        if not isinstance(value, dict) or set(value) != set(schema['properties']):
            fail(label + ': required fields: ' + ', '.join(schema['properties']))

    def number(value, low, high, label):
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            fail(label + ': finite number in ' + str((low, high)))

    def vector(value, low, high, label):
        if not isinstance(value, list) or len(value) != 3:
            fail(label + ': three numbers required')
        for v in value:
            number(v, low, high, label)

    def text(value, label, maximum=2000):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            fail(label + ': nonempty bounded text required')

    fields(value, SCENE_SCHEMA, 'scene')
    text(value['description'], 'description', 8000)
    vector(value['background'], 0, 1, 'background')
    subjects, tracks, scenes = value['subjects'], value['tracks'], value['scenes']
    if not isinstance(subjects, list) or not 1 <= len(subjects) <= 24:
        fail('subjects: provide 1..24 entities')
    ids = set()
    kinds = {}
    for s in subjects:
        fields(s, SCENE_SCHEMA['properties']['subjects']['items'], 'subject')
        ident = s['id']
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', ident) or ident in ids:
            fail('subject id must be unique ASCII identifier')
        ids.add(ident)
        kinds[ident] = s['kind']
        text(s['label'], 'subject label', 120)
        if s['kind'] not in KINDS or s['shape'] not in ('box', 'sphere', 'cylinder'):
            fail('unsupported subject kind or shape')
        vector(s['color'], 0, 1, 'color')
        vector(s['size'], .05, 20, 'size')
        vector(s['position'], -50, 50, 'position')
        if s['position'][2] < 0:
            fail('subject position.z must be at or above ground')
        number(s['yaw'], -720, 720, 'yaw')
    if not isinstance(tracks, list) or len(tracks) > 160:
        fail('tracks: at most 160 actions')
    previous = {}
    for t in tracks:
        fields(t, SCENE_SCHEMA['properties']['tracks']['items'], 'track')
        if t['subject'] not in ids or (t['target'] is not None and t['target'] not in ids):
            fail('unknown subject/target: ' + str(t['subject']) + '/' + str(t['target']))
        if t['target'] == t['subject']:
            fail('action target must be a different entity')
        if t['action'] not in ACTIONS:
            fail('unsupported action; supported: ' + ','.join(ACTIONS))
        if kinds[t['subject']] != 'human' and t['action'] in ('slap', 'point', 'raise_hand', 'wave', 'sit'):
            fail('this articulated action requires a human proxy')
        number(t['start'], 0, duration, 'track.start')
        number(t['end'], 0, duration, 'track.end')
        if t['end'] - t['start'] < 1 / 24 or t['start'] < previous.get(t['subject'], 0) - 1e-6:
            fail('actor tracks must be ordered, nonoverlapping and last at least one frame')
        previous[t['subject']] = t['end']
        vector(t['position'], -50, 50, 'track.position')
        if t['position'][2] < 0:
            fail('track.position.z must be at or above ground')
        number(t['yaw'], -720, 720, 'track.yaw')
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 40:
        fail('scenes: 1..40 intervals required')
    if scene_count and len(scenes) != scene_count:
        fail('scenes must match scenario card count: ' + str(scene_count))
    last = 0
    for s in scenes:
        fields(s, SCENE_SCHEMA['properties']['scenes']['items'], 'scene interval')
        text(s['title'], 'scene title', 120)
        text(s['description'], 'scene description', 4000)
        number(s['start'], 0, duration, 'scene.start')
        number(s['end'], 0, duration, 'scene.end')
        if abs(s['start'] - last) > .001 or s['end'] - s['start'] < 1 / 24:
            fail('scene intervals must continuously cover the duration')
        last = s['end']
        c = s['camera']
        fields(c, CAMERA, 'camera')
        if (not isinstance(c['focus'], list) or not c['focus'] or len(set(c['focus'])) != len(c['focus'])
                or any(v not in ids for v in c['focus'])):
            fail('camera.focus must reference existing unique entities')
        if c['framing'] not in FRAMINGS or c['movement'] not in MOVEMENTS:
            fail('unsupported camera framing or movement')
        number(c['azimuth'], -720, 720, 'azimuth')
        number(c['end_azimuth'], -720, 720, 'end_azimuth')
        number(c['elevation'], CAMERA_ELEVATION['minimum'], CAMERA_ELEVATION['maximum'], 'elevation')
    if abs(last - duration) > .001:
        fail('scenes must end at requested duration')
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
        if kinds[subject] != 'human' and action in ('slap', 'point', 'raise_hand', 'wave', 'sit'):
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
            'camera': {'focus': focus or [s['id'] for s in subjects],
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
