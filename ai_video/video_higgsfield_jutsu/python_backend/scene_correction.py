"""Deterministic post-design corrections; never calls a model."""
from copy import deepcopy
import colorsys
import re
from .resources.scene_props import CATALOG, scene_prop_parts, is_display_prop


def correct_scene(design):
    result = deepcopy(design)
    changes = []
    warnings = []
    subjects = {s['id']: s for s in result['subjects']}
    # Preview identity colors only: independent of AI colors and input ordering.
    # All body parts consume the subject color when the runtime builds the rig.
    people = sorted((s for s in subjects.values() if s['kind'] == 'human'), key=lambda s: s['id'])
    color_changes = []
    for index, subject in enumerate(people):
        color = [round(c, 4) for c in colorsys.hsv_to_rgb(
            (.03 + index * .618033988749895) % 1, .70, .85)]
        before = deepcopy(subject.get('color'))
        subject['color'] = color
        if before != color:
            color_changes.append({'subject': subject['id'], 'field': 'color', 'before': before,
                            'after': color[:], 'reason': 'distinct_previs_actor_color'})
    for subject in subjects.values():
        if subject['kind'] != 'prop' or scene_prop_parts(subject.get('label')) != CATALOG['stage'][1]:
            continue
        if re.search(r'수직|세워|vertical|upright', str(subject.get('label', '')), re.I):
            continue
        before = subject['size'][:]
        # A platform has two broad horizontal axes and a thin vertical axis.
        # Change only the unambiguous case with thickness in the depth slot.
        if before[1] < min(before[0], before[2]) * .15:
            subject['size'] = [before[0], before[2], before[1]]
            changes.append({'subject':subject['id'], 'field':'size', 'before':before,
                            'after':subject['size'][:], 'reason':'horizontal_support_axes'})
    for subject in subjects.values():
        if subject['kind'] != 'prop' or not is_display_prop(subject.get('label')):
            continue
        if re.search(r'눕|수평|바닥|laid|horizontal|floor', str(subject.get('label', '')), re.I):
            continue
        before = subject['size'][:]
        if before[2] < before[1] * .25:
            subject['size'] = [before[0], before[2], before[1]]
            changes.append({'subject': subject['id'], 'field': 'size', 'before': before,
                            'after': subject['size'][:], 'reason': 'upright_blackboard_axes'
                            if scene_prop_parts(subject.get('label')) == CATALOG['blackboard'][1] else 'upright_display_axes'})
    for subject in subjects.values():
        if subject['kind'] != 'human':
            continue
        before = subject['size'][:]
        # Anisotropic human transforms deform every joint and invalidate contact IK.
        # Ambiguous axes cannot tell us the intended stature: use the canonical rig.
        scale = 1.0 if max(before) / min(before) > 1.5 else before[2]
        subject['size'] = [scale] * 3
        if before != subject['size']:
            changes.append({'subject': subject['id'], 'field': 'size', 'before': before,
                            'after': subject['size'][:], 'reason': 'preserve_human_proportions'})
    for actor in subjects.values():
        if actor['kind'] != 'human':
            continue
        support = None
        for track in sorted((t for t in result['tracks'] if t['subject'] == actor['id']),
                            key=lambda t: t['start']):
            if track['action'] in ('move', 'walk', 'run', 'jump', 'fall', 'lie_down', 'fly'):
                support = None
            if track['action'] == 'sit':
                prop = subjects.get(track['target'])
                support = prop if prop and prop['kind'] == 'prop' and prop['shape'] == 'box' else None
                if support is None:
                    warnings.append({'subject': actor['id'], 'reason': 'seat_geometry_unknown'})
            if support is not None:
                before = track['position'][:]
                parts = scene_prop_parts(support.get('label'))
                seating = any(parts == CATALOG[key][1] for key in ('chair', 'bench', 'sofa', 'bed'))
                if parts and not seating:
                    warnings.append({'subject': actor['id'], 'reason': 'target_is_not_known_seating'})
                    support = None
                    continue
                surface = parts[0][1][2] + parts[0][2][2] / 2 if seating else 1.
                minimum_height = .51 * actor['size'][2]
                if surface * support['size'][2] < minimum_height:
                    old_size = support['size'][:]
                    support['size'][2] = minimum_height / surface
                    changes.append({'subject': support['id'], 'field': 'size', 'before': old_size,
                                    'after': support['size'][:], 'reason': 'seat_height_for_actor'})
                top = support['position'][2] + support['size'][2] * surface
                track['position'] = [*support['position'][:2], max(0, top - .51 * actor['size'][2])]
                if track['position'] != before:
                    changes.append({'subject': actor['id'], 'field': 'track.position',
                                    'start': track['start'], 'before': before,
                                    'after': track['position'][:], 'reason': 'seat_support_alignment'})
    return result, {'version': 1, 'changes': changes + color_changes, 'warnings': warnings, 'aiCalls': 0}
