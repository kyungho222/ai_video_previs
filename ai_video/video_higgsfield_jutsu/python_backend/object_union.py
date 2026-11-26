"""Conservatively add explicitly requested catalog props missing from a scene."""
from copy import deepcopy
import re
from .resources.scene_props import CATALOG, scene_prop_parts

# Camera terms usually describe the viewpoint, rather than a physical prop.
EXCLUDED_ALIASES = {'camera', '카메라'}
NEGATION = re.compile(r'^(?:\s|[이가은는을를도와과,])*?(?:없(?:다|는|이|고|음|어)|빼|제외|제거|치우|놓지\s*않|두지\s*않|만들지\s*않)', re.I)


def _mentions(text):
    matches = []
    for key, (aliases, _) in CATALOG.items():
        for alias in aliases:
            if alias in EXCLUDED_ALIASES:
                continue
            if alias.isascii():
                pattern = r'(?<![a-z])' + re.escape(alias) + r's?(?![a-z])'
            elif len(alias) == 1:
                pattern = r'(?<![가-힣])' + re.escape(alias) + r'(?=이|가|을|를|은|는|과|와|도|에|으로|의|[\s,.!?]|$)'
            else:
                pattern = re.escape(alias)
            for match in re.finditer(pattern, text, re.I):
                matches.append((match.start(), match.end(), key, match.group()))
    selected = []
    for start, end, key, word in sorted(matches, key=lambda m: (m[0], -(m[1] - m[0]))):
        if any(start < old[1] and end > old[0] for old in selected):
            continue
        selected.append((start, end, key, word))
    # The last explicit mention is used for revisions; ambiguous negative phrases are ignored.
    result = {}
    for start, end, key, word in selected:
        result[key] = None if NEGATION.match(text[end:end + 35]) else word
    return [(key, word) for key, word in result.items() if word]


PERSON_TYPES = {'남성': 'male', '남자': 'male', '여성': 'female', '여자': 'female',
                'man': 'male', 'men': 'male', 'woman': 'female', 'women': 'female'}
PERSON_PATTERN = re.compile(r'(?<![가-힣A-Za-z])(남성|남자|여성|여자|women|woman|men|man)(?![가-힣A-Za-z])(?:\s*(\d+)\s*(명|인)?)?', re.I)


def extract_requested_people(text):
    """Deduplicate explicit numbered identities and explicit gender counts."""
    result = []
    for match in PERSON_PATTERN.finditer(str(text or '')):
        category = PERSON_TYPES[match.group(1).lower()]
        number = int(match.group(2) or 1)
        if not 1 <= number <= 24 or NEGATION.match(str(text)[match.end():match.end()+35]):
            continue
        indices = range(1, number + 1) if match.group(3) else [number]
        for index in indices:
            entry = (category, index)
            if entry not in result:
                result.append(entry)
    return result


def _known_person(subject):
    text = str(subject.get('id', '')) + ' ' + str(subject.get('label', ''))
    match = re.search(r'(female|woman|women|여성|여자|male|man|men|남성|남자)[_\s-]*(\d+)', text, re.I)
    if not match:
        return None
    category = PERSON_TYPES.get(match.group(1).lower(), match.group(1).lower())
    if category not in ('male', 'female'):
        return None
    return category, int(match.group(2))


def extract_prompt_hints(text):
    """Collect literal evidence as hints; do not treat regex matches as verified relations."""
    text = str(text or '')
    sentences = [part.strip() for part in re.split(r'[\n.!?]+', text) if part.strip()]
    groups = {
        'environments': re.compile(r'교실|학교|거리|도로|무대|공연장|공원|사무실|집 안|실내|실외|바닷가|classroom|stage|street|outdoor|indoor', re.I),
        'relativePositions': re.compile(r'왼쪽|오른쪽|가운데|중앙|옆|뒤쪽|앞쪽|앞에|뒤에|사이|위에|아래|left|right|behind|between|in front', re.I),
        'preservation': re.compile(r'유지|고정|변하지|변경하지|동일하게|keep|preserve|remain|fixed', re.I),
        'actionTargets': re.compile(r'에게|한테|향해|대상으로|toward|towards| to ', re.I),
        'cameraTransitions': re.compile(r'카메라|컷\s*전환|촬영|샷|camera|shot|cut|zoom', re.I),
        'prohibitions': re.compile(r'하지\s*않|않(?:는다|고|도록)|없이|아닌|금지|하지\s*마|제외|no\s+|without|do not', re.I),
    }
    hints = {'people': [{'type': kind, 'number': number} for kind, number in extract_requested_people(text)],
             'objects': [{'type': key, 'text': word} for key, word in _mentions(text)]}
    for key, pattern in groups.items():
        hints[key] = list(dict.fromkeys(sentence[:500] for sentence in sentences if pattern.search(sentence)))[:20]
    return hints


def merge_requested_props(text, design):
    result = deepcopy(design)
    subjects = result.get('subjects', [])
    actor_positions = [s.get('position', [0, 0, 0]) for s in subjects if s.get('kind') in ('human', 'quadruped', 'bird')]
    center_x = sum(p[0] for p in actor_positions) / len(actor_positions) if actor_positions else 0
    rear_y = min((p[1] for p in actor_positions), default=0) - 3
    ids = {s.get('id') for s in subjects}
    represented = {key for key, (_, parts) in CATALOG.items()
                   if any(s.get('kind') == 'prop' and scene_prop_parts(s.get('label')) == parts for s in subjects)}
    added, skipped = [], []
    requested_people = extract_requested_people(text)
    people = [s for s in subjects if s.get('kind') == 'human']
    known = {_known_person(s) for s in people} - {None}
    unknown_count = sum(_known_person(s) is None for s in people)
    missing = [person for person in requested_people if person not in known]
    # Unlabeled actors cannot be assigned an identity safely. Only fill the
    # proven count deficit, without guessing which requested person is missing.
    if unknown_count:
        deficit = max(0, len(missing) - unknown_count)
        missing = [('person', index + 1) for index in range(deficit)]
    for category, number in missing:
        if len(subjects) >= 24:
            skipped.append({'type': 'person', 'label': f'{category} {number}', 'reason': 'subject_limit'})
            continue
        ident = f'requested_{category}_{number}'
        suffix = 2
        while ident in ids:
            ident = f'requested_{category}_{number}_{suffix}'
            suffix += 1
        x = max((s.get('position', [0])[0] for s in people), default=-1.5) + 1.5
        subjects.append({'id': ident, 'label': f'{category} {number}', 'kind': 'human', 'shape': 'sphere',
                         'color': [.5, .5, .5], 'size': [1, 1, 1], 'position': [x, 0, 0], 'yaw': 0})
        people.append(subjects[-1])
        ids.add(ident)
        added.append({'type': 'person', 'label': f'{category} {number}', 'id': ident})
    for key, label in _mentions(str(text or '')):
        if key in represented:
            continue
        if len(subjects) >= 24:
            skipped.append({'type': key, 'label': label, 'reason': 'subject_limit'})
            continue
        ident = 'requested_' + key
        suffix = 2
        while ident in ids:
            ident = 'requested_' + key + '_' + str(suffix)
            suffix += 1
        x = center_x + (len(added) % 5 - 2) * 1.3
        y = rear_y - (len(added) // 5) * 1.3
        size = [3, .2, 1.5] if key == 'blackboard' else [.7, .7, .9]
        subjects.append({'id': ident, 'label': label, 'kind': 'prop', 'shape': 'box',
                         'color': [.4, .4, .4], 'size': size, 'position': [x, y, 0], 'yaw': 0})
        ids.add(ident)
        represented.add(key)
        added.append({'type': key, 'label': label, 'id': ident})
    return result, {'added': added, 'skipped': skipped}
