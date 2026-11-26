"""Reuse stored design data without a camera-analysis AI request."""
import json

def native_timeline(design, duration):
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
        for scene in design['scenes']:
            camera = scene['camera']
            tracks = [{k:t[k] for k in ('subject','action','start','end','position','yaw','target')}
                      for t in design.get('tracks', [])
                      if t['start'] < scene['end'] and t['end'] > scene['start']]
            segments.append({'start':scene['start'], 'end':scene['end'],
                'movement':packed(camera), 'speed':'Follow source timing; connect velocity smoothly.',
                'framing':camera['framing'], 'target':packed(camera['focus']),
                'endState':'end_azimuth='+str(camera['end_azimuth'])+'; framing='+camera['framing'],
                'staging':packed(tracks) if tracks else 'Hold recorded root positions.',
                'events':scene['description'], 'transition':'Continuous unless the user explicitly requests a cut.',
                'startPhysicalState':'Recorded root state; unrecorded contact state is unknown.',
                'endPhysicalState':'Recorded root endpoint; unrecorded contact state is unknown.'})
        value = {'summary':design['description'], 'entities':entities,
                 'uncertainties':['Stored design intent; actual rendered framing adjustments are not remeasured.'],
                 'segments':segments, 'criticalMoments':[]}
        validate_timeline(value,duration)
        return value
    except (KeyError, TypeError, ValueError):
        return None



def scene_brief_for_frame(index, opening_state, current_brief):
    """Opening context is authoritative only for frame zero, never later locations."""
    return opening_state if index == 0 and opening_state else current_brief
