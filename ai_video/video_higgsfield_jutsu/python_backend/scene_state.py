"""Resolve support and root-state transitions before animation and camera execution."""
from copy import deepcopy
import math
import re
from .resources.scene_props import CATALOG, scene_prop_parts
from .scene_contract import action_channel


def normalize_support_relations(design, relations, request):
    objects = {s['id']: s for s in design['subjects']}
    supports, spatial, changes = [], [], []
    for original in relations:
        relation = deepcopy(original)
        target = objects.get(relation.get('target'), {})
        evidence = relation.get('evidence', '')
        parts = scene_prop_parts(target.get('label'))
        surface = next((key for key in ('stage', 'table') if parts == CATALOG[key][1]), None)
        aliases = CATALOG[surface][0] if surface else ()
        explicit_support = (surface and isinstance(evidence, str) and evidence and evidence in request and any(
            re.search(re.escape(alias) + r'(?:에서|\s*위(?:에|에서)?)', evidence, re.I)
            or re.search(r'\bon\s+(?:the\s+)?' + re.escape(alias) + r'\b', evidence, re.I)
            for alias in aliases))
        if relation.get('kind') == 'supported_by':
            supports.append(relation)
        elif explicit_support and relation.get('kind') in ('in_front_of', 'behind'):
            relation.update(kind='supported_by', referenceFrame='world')
            supports.append(relation)
            changes.append({'before':deepcopy(original), 'after':deepcopy(relation),
                            'reason':'support_evidence_is_not_depth'})
        else:
            spatial.append(relation)
    return supports, spatial, changes


def support_top(subject):
    parts = scene_prop_parts(subject.get('label'))
    top = max(p[1][2] + p[2][2]/2 for p in parts) if parts else 1.
    return subject['position'][2] + subject['size'][2] * top


def resolve_scene_state(design, supports):
    result = deepcopy(design)
    objects = {s['id']:s for s in result['subjects']}
    changes, warnings, actors = [], [], {}
    links = {}
    for relation in supports:
        a, b = relation.get('subject'), relation.get('target')
        if a not in objects or b not in objects or a == b:
            warnings.append({'reason':'invalid_support_reference', 'relation':deepcopy(relation)})
            continue
        links.setdefault(a, []).append(b)
    def change(row, position, subject, field, reason):
        before = row['position'][:]
        if before != position:
            row['position'] = position
            changes.append({'subject':subject, 'field':field,'before':before,'after':position[:],'reason':reason})
    for actor in result['subjects']:
        if actor['kind'] != 'human':
            continue
        ident = actor['id']
        tracks = sorted((t for t in result['tracks'] if t['subject'] == ident), key=lambda t:t['start'])
        targets = links.get(ident, [])
        support = objects[targets[0]] if len(targets)==1 else None
        if len(targets)>1:
            warnings.append({'subject':ident,'reason':'ambiguous_support'})
        if support and (support.get('kind')!='prop' or support.get('shape')!='box'
                        or any(t['subject']==support['id'] and t['position']!=support['position'] for t in result['tracks'])):
            warnings.append({'subject':ident,'support':support['id'],'reason':'unsupported_moving_or_nonplanar_support'})
            actors[ident]={'rootAnchor':'feet','supportId':support['id'],'status':'unresolved','transitions':[]}
            continue
        initial_seat = next((t for t in tracks if t['start']==0 and t['action']=='sit' and t.get('initialPosture')), None)
        base = support_top(support) if support else 0 if initial_seat else actor['position'][2]
        state = {'rootAnchor':'feet', 'supportId':support['id'] if support else None,
                 'supportHeight':base, 'status':'resolved' if support or base==0 else 'inherited',
                 'transitions':[]}
        actors[ident]=state
        if support and not initial_seat:
            change(actor,[*actor['position'][:2],base],ident,'position','support_surface_root')
        seated = False
        for t in tracks:
            action = t['action']
            mode = 'grounded'
            if action == 'sit':
                seated = True
                mode = 'seated'
            elif action in ('fly','fall','lie_down'):
                mode = 'airborne' if action=='fly' else 'special_posture'
                seated = False
            elif action == 'jump':
                mode = 'jump_from_support'
                seated = False
            elif action in ('walk','run','move','dance'):
                seated = False
            elif seated:
                mode = 'seated'
            grounded = mode in ('grounded','jump_from_support')
            if grounded and support:
                angle=math.radians(-support.get('yaw',0))
                dx,dy=t['position'][0]-support['position'][0],t['position'][1]-support['position'][1]
                x,y=math.cos(angle)*dx-math.sin(angle)*dy,math.sin(angle)*dx+math.cos(angle)*dy
                if abs(x)>support['size'][0]/2 or abs(y)>support['size'][1]/2:
                    grounded=False
                    mode='unresolved_support_exit'
                    warnings.append({'subject':ident,'start':t['start'],'reason':mode})
            if grounded:
                change(t,[*t['position'][:2],base],ident,'track.position','grounded_root_on_support')
            state['transitions'].append({'start':t['start'],'end':t['end'],'action':action,'mode':mode,
                                         'rootPosition':t['position'][:]})
    return result, {'version':1, 'actors':actors,'supports':deepcopy(supports),
                    'changes':changes,'warnings':warnings,'aiCalls':0}
