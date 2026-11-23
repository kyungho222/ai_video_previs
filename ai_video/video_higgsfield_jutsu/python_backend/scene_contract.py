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
VECTOR = {**array(NUMBER), 'minItems': 3, 'maxItems': 3}
CAMERA = obj({'focus': array(TEXT), 'framing': {'type': 'string', 'enum': list(FRAMINGS)},
              'movement': {'type': 'string', 'enum': list(MOVEMENTS)},
              'azimuth': NUMBER, 'end_azimuth': NUMBER, 'elevation': NUMBER})
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
        number(c['elevation'], -10, 70, 'elevation')
    if abs(last - duration) > .001:
        fail('scenes must end at requested duration')
    return copy.deepcopy(value)


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
