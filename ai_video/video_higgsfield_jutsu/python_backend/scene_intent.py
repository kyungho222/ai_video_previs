"""Source-backed authoring defaults and bounded execution coverage, without AI calls."""
from copy import deepcopy
import math
import re


def source_quote_match(quote, request):
    """Accept line-ending/escaped-newline variants, never changed words."""
    if not isinstance(quote, str) or not quote.strip() or not isinstance(request, str):
        return None
    if quote in request:
        return 'exact'
    normalized = quote.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\r\n', '\n').replace('\r', '\n')
    source = request.replace('\r\n', '\n').replace('\r', '\n')
    return 'newline_normalized' if normalized in source else None


def request_sources(request, history):
    return '\n'.join([str(request or '')] + [i['content'] for i in history
        if isinstance(i, dict) and i.get('role') == 'user' and isinstance(i.get('content'), str)])


def _rows(value):
    return value if isinstance(value, list) else []


def _camera_orbit_quote(request):
    # Require camera context in the same sentence; circular actor motion is not
    # evidence for a camera orbit. Preserve the model's path, not guessed angles.
    for sentence in re.split(r'[.!?\n]', str(request or '')):
        if (re.search(r'카메라|camera', sentence, re.I)
                and re.search(r'원형|공전|주위를?\s*(?:돌|회전)|orbit|circular', sentence, re.I)
                and not re.search(r'금지|하지\s*(?:않|말)|no\s+orbit|do\s+not', sentence, re.I)):
            return sentence.strip()
    return None


def _faces_camera(text):
    if re.search(r'(?:do\s+not|don.t|never)\s+(?:face|look)|(?:바라보|응시)지\s*(?:않|말)', str(text or ''), re.I):
        return False
    return bool(re.search(r'(?:카메라|렌즈)(?:를|을)?\s*(?:정면으로\s*)?(?:바라|응시|향해\s*(?:몸을\s*)?(?:돌|서))|'
                          r'fac(?:e|es|ing)\s+(?:the\s+)?camera|look\w*\s+(?:at|toward)\s+(?:the\s+)?camera',
                          str(text or ''), re.I))


def prepare_authored_direction(value, request):
    result, changes = deepcopy(value), []
    if not isinstance(result, dict):
        return result, changes
    headings = {s['id']: s.get('yaw', 0) for s in _rows(result.get('subjects'))
                if isinstance(s, dict) and isinstance(s.get('id'), str)}
    for i, beat in enumerate(_rows(result.get('beats'))):
        if not isinstance(beat, dict):
            continue
        quote = beat.get('timingEvidence')
        seconds = beat.get('seconds')
        numbers = re.findall(r'(\d+(?:\.\d+)?)\s*(?:초|seconds?\b|secs?\b|s\b)', quote if isinstance(quote, str) else '', re.I)
        verified = bool(source_quote_match(quote, request)) and any(
            type(seconds) in (int, float) and math.isfinite(seconds) and math.isclose(float(n), seconds) for n in numbers)
        source_backed = bool(source_quote_match(quote, request))
        if beat.get('purpose') == 'wait' and not source_backed:
            beat['purpose'] = 'introduction'
            changes.append({'beat': i, 'reason': 'unverified_wait_removed', 'source': 'model'})
        if seconds is not None and not verified:
            beat['seconds'] = None
            changes.append({'beat': i, 'reason': 'unverified_timing_to_relative', 'before': seconds,
                            'source': 'model', 'after': None})
        camera = beat.get('camera')
        if isinstance(camera, dict):
            camera.setdefault('transition', 'continuous')
            actions = [a for a in _rows(beat.get('actions')) if isinstance(a, dict)]
            evidence = camera.get('directionEvidence')
            orbit_quote = _camera_orbit_quote(request) if camera.get('movement') == 'orbit' else None
            if orbit_quote and not source_quote_match(evidence, request):
                camera['directionEvidence'] = orbit_quote
                changes.append({'beat': i, 'reason': 'source_camera_movement', 'source': 'user', 'sourceQuote': orbit_quote})
            if not source_quote_match(camera.get('directionEvidence'), request):
                primary = next((a.get('subject') for a in actions if a.get('role') in ('speaker', 'performer') and isinstance(a.get('subject'), str)), None)
                yaw = headings.get(primary, 0)
                front = -yaw if type(yaw) in (int, float) and math.isfinite(yaw) else 0
                before = {k: camera.get(k) for k in ('azimuth', 'end_azimuth', 'movement')}
                camera.update(azimuth=front, end_azimuth=front)
                if camera.get('movement') == 'orbit':
                    camera['movement'] = 'locked'
                end_yaw = next((a.get('yaw') for a in actions if a.get('subject') == primary), yaw)
                if type(end_yaw) in (int, float) and type(yaw) in (int, float) and abs(end_yaw - yaw) > 1e-6:
                    changes.append({'beat': i, 'subject': primary, 'reason': 'front_start_only_for_turn', 'source': 'default'})
                if any(camera.get(k) != v for k, v in before.items()):
                    changes.append({'beat': i, 'reason': 'default_front_camera', 'source': 'default', 'before': before})
        turns = [a for a in _rows(beat.get('actions')) if isinstance(a, dict)
                 and a.get('action') == 'turn' and a.get('role') in ('performer', 'speaker')]
        for action in _rows(beat.get('actions')):
            if isinstance(action, dict) and isinstance(action.get('subject'), str):
                # A fixed world yaw cannot express facing a moving camera.
                # Only bind the named turn/performer, never every observer.
                action.pop('faceCamera', None)
                if (_faces_camera(request) and _faces_camera(beat.get('intent'))
                        and action in turns and (len(turns) == 1 or action.get('target') == 'camera')):
                    action['faceCamera'] = True
                    changes.append({'beat': i, 'subject': action['subject'], 'reason': 'camera_relative_facing', 'source': 'user'})
                headings[action['subject']] = action.get('yaw', headings.get(action['subject'], 0))
    # Source-linked constraints take precedence over generated defaults. Conflicting
    # explicit constraints remain visible in the later report rather than disappearing.
    beats = _rows(result.get('beats'))
    for requirement in _rows(result.get('requirements')):
        if not isinstance(requirement, dict) or requirement.get('kind') != 'camera':
            continue
        quote, value = requirement.get('sourceQuote'), requirement.get('value')
        if not source_quote_match(quote, request):
            continue
        key = 'transition' if value in ('cut', 'continuous') else 'framing' if value in ('wide', 'full', 'medium', 'close') else None
        if key is None:
            continue
        for index in _rows(requirement.get('beats')):
            if type(index) is not int or not 0 <= index < len(beats) or not isinstance(beats[index], dict):
                continue
            camera = beats[index].get('camera')
            if isinstance(camera, dict) and camera.get(key) != value:
                before = camera.get(key)
                camera[key] = value
                changes.append({'beat': index, 'reason': 'source_camera_constraint', 'source': 'user',
                                'sourceQuote': quote, 'field': key, 'before': before, 'after': value})
    return result, changes


def assess_intent(direction, design, runtime, request):
    """Coverage checks prove executable representation, never full text/video equivalence."""
    from .scene_direction import direction_candidate
    issues, requirements = [], []
    direction = direction if isinstance(direction, dict) else {}
    beats = _rows(direction.get('beats'))
    tracks, scenes = design.get('tracks', []), design.get('scenes', [])
    duration = max((s.get('end', 0) for s in scenes), default=0)
    expected = direction_candidate(direction, duration) if beats else {'tracks': [], 'scenes': []}
    # Exact intervals disambiguate repeated gestures by the same actor.
    for track in expected['tracks']:
        if track.get('action') == 'idle':
            continue
        actor, motion = track.get('subject'), track.get('action')
        matching = [t for t in tracks if t.get('subject') == actor and t.get('action') == motion
                    and abs(t['start'] - track['start']) < 1e-6 and abs(t['end'] - track['end']) < 1e-6]
        if not matching:
            issues.append({'kind': 'action_missing', 'subject': actor, 'action': motion,
                           'start': track['start'], 'end': track['end']})
            continue
        covered = sum(max(0, min(track['end'], s['end']) - max(track['start'], s['start']))
                      for s in scenes if actor in s.get('camera', {}).get('focus', []))
        if covered < track['end'] - track['start'] - 1 / 24:
            issues.append({'kind': 'action_off_camera', 'subject': actor,
                           'start': track['start'], 'end': track['end']})
    for i, shot in enumerate(expected['scenes']):
        actual = next((s for s in scenes if abs(s['start'] - shot['start']) < 1e-6
                       and abs(s['end'] - shot['end']) < 1e-6), None)
        for key in ('focus', 'framing', 'transition', 'movement', 'azimuth', 'end_azimuth', 'elevation'):
            default = 'continuous' if key == 'transition' else None
            if actual is None or actual['camera'].get(key, default) != shot['camera'].get(key, default):
                issues.append({'kind': 'camera_instruction_changed', 'shot': i, 'field': key})
    actual_ids = {s['id'] for s in design.get('subjects', [])}
    for subject in _rows(direction.get('subjects')):
        if isinstance(subject, dict) and isinstance(subject.get('id'), str) and subject['id'] not in actual_ids:
            issues.append({'kind': 'subject_missing', 'subject': subject['id']})
    coverage_failed = bool(issues)
    for row in _rows(direction.get('requirements')):
        if not isinstance(row, dict):
            continue
        quote, indices = row.get('sourceQuote'), row.get('beats', [])
        quote_match = source_quote_match(quote, request)
        valid = bool(quote_match)
        valid_links = isinstance(indices, list) and all(type(i) is int and 0 <= i < len(beats) for i in indices)
        subjects = row.get('subjects', [])
        subjects_valid = isinstance(subjects, list) and all(isinstance(v, str) for v in subjects)
        status = 'unknown'
        if not subjects_valid:
            status = 'mismatch'
            subjects = []
            issues.append({'kind': 'invalid_requirement_subjects', 'id': row.get('id')})
        if row.get('kind') in ('camera', 'action') and not indices:
            status = 'mismatch'
            issues.append({'kind': 'requirement_not_represented', 'id': row.get('id')})
        if not valid or not valid_links:
            status = 'mismatch'
            issues.append({'kind': 'invalid_source_quote' if not valid else 'invalid_requirement_link', 'id': row.get('id')})
        if status != 'mismatch' and row.get('value') and valid_links and indices:
            value = row['value']
            windows = [s for s in expected['scenes'] if s.get('sourceBeat') in indices]
            selected = [s for s in scenes if any(abs(s['start'] - w['start']) < 1e-6 and abs(s['end'] - w['end']) < 1e-6 for w in windows)]
            key = 'framing' if value in ('close', 'medium', 'full', 'wide') else 'transition' if value in ('cut', 'continuous') else None
            if key:
                represented = bool(selected) and all(s['camera'].get(key, 'continuous' if key == 'transition' else None) == value for s in selected)
                represented = represented and all(any(subject in s['camera'].get('focus', []) for s in selected) for subject in subjects)
                if value == 'close' and len(subjects) == 1:
                    represented = represented and all(s['camera'].get('focus') == subjects for s in selected)
                if not represented:
                    status = 'mismatch'
                    issues.append({'kind': 'requirement_not_represented', 'id': row.get('id'), 'value': value, 'subjects': subjects})
                else:
                    # Only a measured camera can confirm camera requirements; other semantic claims stay unknown.
                    measured = runtime.get('cameraQuality') or {}
                    status = 'matched' if measured.get('status') == 'matched' else 'unknown'
            elif row.get('kind') == 'action':
                represented = all(any(t['action'] == value and t['subject'] == subject for t in tracks
                                     if any(t['start'] < s['end'] and t['end'] > s['start'] for s in selected)) for subject in subjects)
                if not represented:
                    status = 'mismatch'
                    issues.append({'kind': 'requirement_not_represented', 'id': row.get('id'), 'value': value, 'subjects': subjects})
        requirements.append({**row, 'subjects': subjects, 'status': status, 'sourceQuoteMatch': quote_match,
                             'representation': 'linked' if valid_links and indices else 'unlinked'})
    measured = runtime.get('cameraQuality') or {}
    issues.extend(measured.get('issues', []))
    projection = runtime.get('projectionWarnings') or []
    if projection and not measured.get('issues'):
        issues.append({'kind': 'framing', 'frames': len(projection), 'details': projection})
    if not requirements:
        requirements.append({'id': 'request', 'status': 'unknown', 'reason': 'No source-linked requirements available'})
    return {'status': 'mismatch' if issues else 'unknown', 'issues': issues, 'requirements': requirements,
            'coverage': {'status': 'mismatch' if coverage_failed else ('matched' if expected['scenes'] else 'unknown')},
            'camera': measured or {'status': 'unknown'},
            'semanticStatus': 'unknown', 'reason': 'Geometry checks do not validate dialogue, appearance or setting semantics'}
