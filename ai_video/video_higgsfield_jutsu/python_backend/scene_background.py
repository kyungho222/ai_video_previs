"""Move only unconstrained secondary backdrop geometry out of primary motion envelopes."""
from copy import deepcopy
import math
from .resources.scene_props import CATALOG, scene_prop_parts
from .resources.scene_visibility_policy import scene_visibility_roles


def _bounds(subject):
    parts=scene_prop_parts(subject.get('label')) if subject.get('kind')=='prop' else None
    if subject.get('kind')!='prop':
        parts=[('box',(0,0,.97),(.8,.65,1.94),1)]
    parts=parts or [('box',(0,0,.5),(1,1,1),1)]
    angle=math.radians(subject.get('yaw',0)); c,s=math.cos(angle),math.sin(angle)
    points=[]
    for _,pos,size,_ in parts:
        for a in (-.5,.5):
            for b in (-.5,.5):
                for z in (-.5,.5):
                    x,y=(pos[0]+a*size[0])*subject['size'][0],(pos[1]+b*size[1])*subject['size'][1]
                    points.append([subject['position'][0]+c*x-s*y,subject['position'][1]+s*x+c*y,
                                   subject['position'][2]+(pos[2]+z*size[2])*subject['size'][2]])
    return [min(p[i] for p in points) for i in range(3)],[max(p[i] for p in points) for i in range(3)]


def prevent_background_intrusions(design, relations):
    result=deepcopy(design); roles=scene_visibility_roles(result)
    protected=set(roles['primary']); occupied=[]; changes=[]; warnings=[]
    for subject in result['subjects']:
        if subject['id'] not in protected:
            continue
        points=[subject['position']]+[t['position'] for t in result.get('tracks',[]) if t['subject']==subject['id']
            and (t.get('channel') in ('movement','posture') or 'channel' not in t)]
        bounds=[_bounds({**subject,'position':p}) for p in points]
        occupied.append(([min(b[0][i] for b in bounds) for i in range(3)],
                         [max(b[1][i] for b in bounds) for i in range(3)]))
    constrained={r.get('subject') for r in relations}|{r.get('target') for r in relations}
    angle=math.radians((result.get('scenes') or [{}])[0].get('camera',{}).get('azimuth',0))
    depth=[math.sin(angle),math.cos(angle)]
    def overlap(a,b):
        return all(a[0][i]<b[1][i]+.1 and b[0][i]<a[1][i]+.1 for i in range(3))
    def depth_limits(bounds):
        low,high=bounds
        values=[x*depth[0]+y*depth[1] for x in (low[0],high[0]) for y in (low[1],high[1])]
        return min(values),max(values)
    for obj in result['subjects']:
        if obj['id'] not in roles['secondary'] or not occupied:
            continue
        parts=scene_prop_parts(obj.get('label'))
        if not any(parts==CATALOG[key][1] for key in ('wall','curtain','graffiti','neon','truss')):
            continue
        bounds=_bounds(obj)
        if not any(overlap(bounds,b) for b in occupied):
            continue
        if obj['id'] in constrained or any(t['subject']==obj['id'] for t in result.get('tracks',[])):
            warnings.append({'id':obj['id'],'reason':'background_intrusion_with_authored_constraint'})
            continue
        delta=min(depth_limits(b)[0] for b in occupied)-.5-depth_limits(bounds)[1]
        before=obj['position'][:]
        after=[before[0]+delta*depth[0],before[1]+delta*depth[1],before[2]]
        candidate={**obj,'position':after}
        if abs(delta)>20 or any(abs(v)>50 for v in after) or any(overlap(_bounds(candidate),b) for b in occupied):
            warnings.append({'id':obj['id'],'reason':'background_intrusion_exceeds_correction_bounds'})
            continue
        obj['position']=after
        changes.append({'id':obj['id'],'before':before,'after':after,'reason':'secondary_background_clear_primary_envelope'})
    return result,{'roles':roles,'changes':changes,'warnings':warnings,'aiCalls':0}
