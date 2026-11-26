"""Reusable prop dimensions and open environment blocking, without model calls."""
import math
import re
from .resources.scene_props import scene_prop_parts, CATALOG

# width, depth, height (metres); only used for missing, Python-created props.
PROFILES = {
    'blackboard': ([3, .2, 1.5], [.12, .3, .2]),
    'display': ([3.2, .15, 1.8], [.25, .5, .65]),
    'table': ([1.3, .75, .8], [.5, .35, .2]),
    'chair': ([.55, .55, 1], [.4, .35, .3]),
    'stage': ([6, 4, .4], [.25, .27, .32]),
    'wall': ([8, .2, 3.2], [.55, .6, .65]),
    'window': ([1.4, .15, 1.6], [.5, .65, .75]),
    'door': ([.9, .15, 2.1], [.45, .3, .2]),
    'neon': ([3, .1, 2.3], [.4, .6, .9]),
    'graffiti': ([3, .1, 1.6], [.65, .4, .7]),
    'spotlight': ([.6, .6, 2.5], [.5, .55, .65]),
    'speaker': ([.65, .55, 1.3], [.25, .27, .3]),
    'curtain': ([3, .2, 3], [.5, .2, .25]),
    'truss': ([3, .25, 3], [.55, .6, .65]),
    'microphone': ([.12, .12, .3], [.3, .3, .35]),
    'cup': ([.1, .1, .12], [.65, .65, .7]),
    'bottle': ([.09, .09, .28], [.4, .6, .5]),
    'phone': ([.08, .015, .16], [.3, .3, .35]),
    'book': ([.2, .25, .04], [.5, .3, .25]),
}


def default_prop_spec(key):
    size, color = PROFILES.get(key, ([.7, .7, .9], [.4, .4, .4]))
    return list(size), list(color)


def placement_basis(design):
    people = [s for s in design.get('subjects', []) if s.get('kind') in ('human','quadruped','bird')]
    points = [s['position'] for s in people]
    ids = {s['id'] for s in people}
    points += [t['position'] for t in design.get('tracks', []) if t.get('subject') in ids]
    points = points or [[0,0,0]]
    shots = design.get('scenes', [])
    angle = math.radians(shots[0].get('camera', {}).get('azimuth', 0)) if shots else 0
    right, depth = [math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]
    xs = [p[0]*right[0]+p[1]*right[1] for p in points]
    ys = [p[0]*depth[0]+p[1]*depth[1] for p in points]
    return {'right':right, 'depth':depth, 'center':(min(xs)+max(xs))/2,
            'rear':min(ys)-3, 'width':max(6, max(xs)-min(xs)+4),
            'yaw':-math.degrees(angle)}


def prop_position(key, index, design):
    basis = placement_basis(design)
    x = basis['center'] + (index % 5 - 2) * 1.5
    y = basis['rear'] - (index // 5) * 1.5
    z = 0
    if key == 'stage':
        # Top of the catalog platform is .42 of its local unit box.
        x, y, z = basis['center'], basis['rear'] + 3, -.42 * default_prop_spec(key)[0][2]
    elif key in ('blackboard','display','window','graffiti'):
        z = .8
    return [x*basis['right'][0]+y*basis['depth'][0],
            x*basis['right'][1]+y*basis['depth'][1],z], basis['yaw']


def add_environment_backdrop(request, design):
    """One open rear wall for explicit interiors; no enclosing box or invented furniture."""
    text = str(request or '')
    if (not re.search(r'교실|방송국|방송\s*스튜디오|classroom|broadcast(?:ing)? studio', text, re.I)
            or re.search(r'벽.{0,12}(?:없|제외|제거)|배경.{0,12}(?:없|제외|제거)|without (?:a )?(?:wall|background)|no walls?', text, re.I)):
        return []
    subjects = design['subjects']
    if len(subjects) >= 24 or any(s.get('kind') == 'prop' and
            (scene_prop_parts(s.get('label')) == CATALOG['wall'][1] or
             re.search(r'classroom|studio|교실|스튜디오', str(s.get('label','')), re.I)) for s in subjects):
        return []
    basis = placement_basis(design)
    ident = 'environment_backdrop'
    ids = {s['id'] for s in subjects}
    while ident in ids:
        ident += '_1'
    subjects.append({'id':ident, 'label':'background wall', 'kind':'prop', 'shape':'box',
        'size':[basis['width']+2,.2,3.2], 'position':[
            basis['center']*basis['right'][0]+(basis['rear']-1)*basis['depth'][0],
            basis['center']*basis['right'][1]+(basis['rear']-1)*basis['depth'][1],0],
        'color':[.55,.6,.65], 'yaw':basis['yaw']})
    return [{'id':ident, 'type':'wall', 'reason':'explicit_interior_open_backdrop'}]
