"""Main subjects win over secondary decor; require measured repeated body occlusion."""
import math as _visibility_math


def scene_visibility_roles(plan):
    ids={s['id'] for s in plan['subjects']}
    primary={s['id'] for s in plan['subjects'] if s.get('kind')!='prop'}
    primary.update(key for shot in plan.get('scenes',[]) for key in shot.get('camera',{}).get('focus',[]) if key in ids)
    primary.update(t['subject'] for t in plan.get('tracks',[]) if t.get('action') not in (None,'idle'))
    primary.update(t['target'] for t in plan.get('tracks',[]) if t.get('target') in ids)
    return {'primary':sorted(primary & ids), 'secondary':sorted(ids-primary)}


def secondary_occlusion_candidates(plan, report, fps, object_names):
    roles=scene_visibility_roles(plan)
    secondary=set(roles['secondary']); primary=set(roles['primary'])
    if not report.get('checked') or type(fps) not in (int,float) or not _visibility_math.isfinite(fps) or fps<=0:
        return []
    found={}
    for row in report.get('subjects',[]):
        if object_names.get(row.get('subject'),row.get('subject')) not in primary:
            continue
        tested=row.get('testedFrames',0)
        if type(tested) is not int or tested<=0:
            continue
        counts=row.get('bodyOccluderFrames',{})
        runs=row.get('bodyOccluderLongestRun',{})
        for name,count in counts.items():
            ident=object_names.get(name)
            if ident not in secondary or type(count) is not int:
                continue
            longest=runs.get(name,0)
            repeated=count>=max(2,_visibility_math.ceil(fps*.5)) and (
                count/tested>=.3 or (type(longest) is int and longest>=fps and count/tested>=.1))
            if repeated and (ident not in found or count>found[ident]['blockedFrames']):
                found[ident]={'id':ident,'occluder':name,'blockedFrames':count,'testedFrames':tested,
                              'longestRun':longest,'reason':'repeated_primary_body_occlusion'}
    return [found[key] for key in sorted(found)]
