"""Reuse stored design data without a camera-analysis AI request."""
import json
import math


def _rendered_samples(runtime, shot, index):
    composition = runtime.get('screenComposition', {}) if isinstance(runtime, dict) else {}
    rows = composition.get('samples', []) if isinstance(composition, dict) else []
    def finite_vector(value, count):
        return isinstance(value,list) and len(value)==count and all(type(v) in (int,float) and math.isfinite(v) for v in value)
    valid = []
    for row in rows if isinstance(rows,list) else []:
        if (not isinstance(row,dict) or row.get('shot') != index
                or type(row.get('time')) not in (int,float) or not math.isfinite(row['time'])
                or not shot['start'] <= row['time'] < shot['end']
                or not isinstance(row.get('camera'),dict)
                or not finite_vector(row['camera'].get('position'),3)):
            continue
        objects = []
        for obj in row.get('subjects', []) if isinstance(row.get('subjects'),list) else []:
            if not isinstance(obj,dict) or not isinstance(obj.get('id'),str):
                continue
            objects.append({'id':obj['id'], 'center':obj.get('center') if finite_vector(obj.get('center'),2) else None,
                            'inFrame':obj.get('intersectsFrame') if type(obj.get('intersectsFrame')) is bool else None,
                            'inFront':obj.get('inFront') if type(obj.get('inFront')) is bool else None})
        if objects:
            valid.append({'time':row['time'],'camera':row['camera'],'objects':objects})
    valid.sort(key=lambda row:row['time'])
    return [valid[i] for i in sorted({0,len(valid)//2,len(valid)-1})] if valid else []


def screen_order_changes(runtime):
    """Report measured changes, not whether a requested camera orbit is wrong."""
    rows = (runtime.get('screenComposition') or {}).get('samples', []) if isinstance(runtime,dict) else []
    result, previous = [], {}
    for row in rows:
        objects = [o for o in row.get('subjects', []) if o.get('inFront') is True and o.get('intersectsFrame') is True
                   and isinstance(o.get('center'),list)]
        objects.sort(key=lambda obj:obj['id'])
        for i,a in enumerate(objects):
            for b in objects[i+1:]:
                delta = b['center'][0]-a['center'][0]
                if abs(delta)<.03:
                    continue
                key=(a['id'],b['id']); sign=1 if delta>0 else -1
                if key in previous and previous[key][0]!=sign:
                    result.append({'subjects':list(key), 'beforeTime':previous[key][1], 'time':row['time'],
                                   'reason':'measured_screen_order_changed', 'intentStatus':'not_judged'})
                previous[key]=(sign,row['time'])
    return result

def native_timeline(design, duration, *, runtime=None):
    """Reuse stored design intent; never claim unmeasured contact/camera metrics."""
    from .previs_timeline import validate_timeline
    if not isinstance(design, dict) or not all(design.get(k) for k in ('subjects', 'scenes')):
        return None
    def packed(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    try:
        entities = [packed({k: subject[k] for k in ('id','label','kind','position','yaw')})
                    for subject in design['subjects']]
        segments = []
        measured_count = 0
        for index, scene in enumerate(design['scenes']):
            camera = scene['camera']
            tracks = [{k:t[k] for k in ('subject','action','start','end','position','yaw','target')}
                      for t in design.get('tracks', [])
                      if t['start'] < scene['end'] and t['end'] > scene['start']]
            for track, original in zip(tracks, [t for t in design.get('tracks', [])
                      if t['start'] < scene['end'] and t['end'] > scene['start']]):
                if original.get('channel') in ('gesture','vocal'):
                    track.pop('position',None)
                    track.pop('yaw',None)
                    track['rootMotion'] = False
            segments.append({'start':scene['start'], 'end':scene['end'],
                'movement':packed(camera), 'speed':'Follow source timing; connect velocity smoothly.',
                'framing':camera['framing'], 'target':packed(camera['focus']),
                'endState':'end_azimuth='+str(camera['end_azimuth'])+'; framing='+camera['framing'],
                'staging':packed(tracks) if tracks else 'Hold recorded root positions.',
                'events':scene['description'], 'transition':'Continuous unless the user explicitly requests a cut.',
                'startPhysicalState':'Recorded root state; unrecorded contact state is unknown.',
                'endPhysicalState':'Recorded root endpoint; unrecorded contact state is unknown.'})
            measured = _rendered_samples(runtime,scene,index)
            if measured:
                measured_count += 1
                segment=segments[-1]
                segment['movement']=packed({'rendered_camera':[{'time':r['time'], **r['camera']} for r in measured],
                                            'motionType':camera['movement']})
                screen_states=[]
                for row in measured:
                    objects={obj['id']:obj for obj in row['objects']}
                    points=[]
                    for track in tracks:
                        a,b=objects.get(track['subject']),objects.get(track.get('target'))
                        if (track['action']=='point' and track['start'] <= row['time'] <= track['end']
                                and a and b and a['inFront'] is True and b['inFront'] is True
                                and a['center'] and b['center']):
                            delta=b['center'][0]-a['center'][0]
                            points.append({'subject':track['subject'],'target':track['target'],
                                           'direction':'screen_right' if delta>.03 else 'screen_left' if delta<-.03 else 'screen_center'})
                    screen_states.append({'time':row['time'],'objects':row['objects'],'pointing':points})
                segment['staging']=packed({'actions':tracks,'viewer_screen':screen_states,
                    'coordinates':'x=0 screen-left, x=1 screen-right, y=0 top; not actor-left/right; occlusion unknown'})
                segment['endState']='Rendered viewer_screen end state: '+packed(screen_states[-1]['objects'])
                segment['transition']='cut' if camera.get('transition')=='cut' else 'continuous; follow measured camera samples'
        value = {'summary':design['description'], 'entities':entities,
                 'uncertainties':(['Screen layout and camera measured from final Blender geometry; occlusion not inferred.'
                                   + (' Some segments lack measurements and retain design intent.' if measured_count<len(segments) else '')]
                                  if measured_count else ['Stored design intent; actual rendered framing adjustments are not remeasured.']),
                 'segments':segments, 'criticalMoments':[]}
        validate_timeline(value,duration)
        return value
    except (KeyError, TypeError, ValueError):
        return None



def scene_brief_for_frame(index, opening_state, current_brief):
    """Opening context is authoritative only for frame zero, never later locations."""
    return opening_state if index == 0 and opening_state else current_brief
