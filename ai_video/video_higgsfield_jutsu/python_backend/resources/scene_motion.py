"""Pure Python animation sampling; the same code runs locally and in Blender."""
import math as _scene_math


def _scene_mix(a, b, u):
    return [x + (y - x) * u for x, y in zip(a, b)]


def face_camera_yaw(position, camera_position, yaw, weight=1.):
    """+Y is actor front; use evaluated camera XY, preserving vertical pose."""
    dx, dy = camera_position[0] - position[0], camera_position[1] - position[1]
    if dx * dx + dy * dy < 1e-12:
        return yaw
    target = _scene_math.degrees(_scene_math.atan2(-dx, dy))
    delta = (target - yaw + 180) % 360 - 180
    return yaw + delta * max(0., min(1., weight))


def camera_step(previous, desired, velocity, dt, max_speed, max_acceleration):
    """Continuous camera motion in world units, independent of Blender APIs."""
    def limit(vector, maximum):
        length = sum(v * v for v in vector) ** .5
        return [v * min(1., maximum / max(length, 1e-9)) for v in vector]
    wanted = limit([(b - a) / .25 for a, b in zip(previous, desired)], max_speed)
    change = limit([a - b for a, b in zip(wanted, velocity)], max_acceleration * dt)
    speed = limit([a + b for a, b in zip(velocity, change)], max_speed)
    return [a + b * dt for a, b in zip(previous, speed)], speed


def _sample_single_track(plan, subject_id, time):
    subject = next(s for s in plan['subjects'] if s['id'] == subject_id)
    position, yaw = subject['position'][:], subject['yaw']
    last_action, last_target = 'idle', None
    posture = None
    for track in plan['tracks']:
        if track['subject'] != subject_id:
            continue
        if time < track['start']:
            break
        span = track['end'] - track['start']
        u = min(1., max(0., (time - track['start']) / span))
        smooth = u * u * (3 - 2 * u)
        current = _scene_mix(position, track['position'], smooth)
        current_yaw = yaw + (track['yaw'] - yaw) * smooth
        if time < track['end']:
            return {'position': current, 'yaw': current_yaw, 'action': track['action'],
                    'phase': 1. if track.get('initialPosture') else u, 'seconds': time - track['start'], 'target': track['target'],
                    'posture': posture if track['action'] in ('idle', 'speak', 'turn', 'wave', 'wave_both', 'hand_heart', 'clap', 'salute', 'handshake', 'push', 'pull', 'arms_up', 'raise_hand', 'point', 'reach', 'hold', 'slap') else None}
        position, yaw = track['position'][:], track['yaw']
        last_action, last_target = track['action'], track['target']
        if last_action in ('sit', 'crouch'):
            posture = last_action
        elif last_action in ('move', 'walk', 'run', 'jump', 'fall', 'lie_down', 'fly'):
            posture = None
    # Pose states persist between action intervals; cyclic gestures stop at the endpoint.
    action = last_action if last_action in ('sit', 'crouch', 'fall', 'lie_down', 'hold', 'raise_hand', 'hand_heart', 'arms_up', 'point', 'reach') else 'idle'
    return {'position': position, 'yaw': yaw, 'action': action, 'phase': 1., 'seconds': 0., 'target': last_target, 'posture': posture}


def sample_track(plan, subject_id, time):
    tracks = [t for t in plan['tracks'] if t.get('subject') == subject_id]
    if not any('channel' in t for t in tracks):
        return _sample_single_track(plan, subject_id, time)
    root_tracks = [t for t in tracks if t.get('channel') in ('movement', 'posture')]
    if not root_tracks:
        root_tracks = [t for t in tracks if t.get('channel') in ('gesture', 'vocal')]
    primary = _sample_single_track({**plan, 'tracks': root_tracks}, subject_id, time)
    for channel in ('gesture', 'vocal'):
        active = [t for t in tracks if t.get('channel') == channel and t['start'] <= time < t['end']]
        if active:
            track = active[0]
            sample = _sample_single_track({**plan, 'tracks': [track]}, subject_id, time)
            primary[channel + '_action'] = sample['action']
            primary[channel + '_phase'] = sample['phase']
            primary[channel + '_seconds'] = sample['seconds']
            if channel == 'gesture':
                primary['gesture_target'] = sample['target']
    return primary


def action_target(sample):
    # An empty gesture target is intentional; do not inherit the posture target.
    if sample.get('gesture_action') is not None:
        return sample.get('gesture_target')
    return sample.get('target')


def pointing_goal(target, shoulders, reach=.56):
    """Select a reachable arm endpoint on the ray from shoulder to target."""
    side = min(shoulders, key=lambda key: sum((a-b)**2 for a,b in zip(target,shoulders[key])))
    shoulder = shoulders[side]
    direction = [a-b for a,b in zip(target, shoulder)]
    distance = sum(v*v for v in direction)**.5
    length = min(reach, distance)
    hand = [a + v * length / max(distance, 1e-9) for a,v in zip(shoulder,direction)]
    def intersects_body(point):
        return abs(point[0]) < .22 and abs(point[1]) < .14 and .9 < point[2] < 1.65
    avoidance = any(intersects_body([a+(b-a)*i/10 for a,b in zip(shoulder,hand)]) for i in range(1,11))
    if avoidance:
        # Keep an obstructed gesture outside the torso. The resulting angular
        # compromise is reported by the renderer rather than moving the actor.
        safe = [-.34 if side == 'L' else .34, .28, max(.9, min(1.7, hand[2]))]
        delta = [b-a for a,b in zip(shoulder,safe)]
        norm = sum(v*v for v in delta)**.5
        hand = [a+v*min(1.,reach/max(norm,1e-9)) for a,v in zip(shoulder,delta)]
    return {'side':side, 'hand':hand, 'targetDistance':distance, 'contactRequired':False,
            'avoidanceApplied':avoidance}


def human_pose(sample):
    """Return connected joint targets in local metres; shoulders/hips never detach."""
    action, u, seconds = sample['action'], sample['phase'], sample['seconds']
    gait = _scene_math.sin(seconds * (13 if action == 'run' else 8)) if action in ('walk', 'run') else 0
    crouch = .42 * (u if action in ('sit', 'crouch') else 1 if sample.get('posture') in ('sit', 'crouch') else 0)
    arm_action = sample.get('gesture_action') or (sample.get('vocal_action') if action in ('idle', 'sit', 'crouch', 'walk', 'run', 'move', 'turn') else None) or action
    arm_u = sample.get('gesture_phase', sample.get('vocal_phase', u))
    arm_seconds = sample.get('gesture_seconds', sample.get('vocal_seconds', seconds))
    joints = {}
    for side, sign in (('L', -1), ('R', 1)):
        step = gait * sign * (.27 if action == 'run' else .16)
        joints[side + '_Hip'] = [sign * .13, 0, .93 - crouch]
        joints[side + '_Foot'] = [sign * .13, step, .08 + max(0, step) * .6]
        if side == 'R' and action in ('kick', 'stomp'):
            arc = max(0., _scene_math.sin(_scene_math.pi * u))
            joints[side + '_Foot'] = [sign * .13, .42 * arc if action == 'kick' else 0, .08 + (.25 if action == 'kick' else .22) * arc]
        elif side == 'L' and action in ('kick_left', 'stomp_left', 'step_side', 'step_back', 'foot_tap'):
            arc = max(0., _scene_math.sin(_scene_math.pi * u))
            x = sign * (.13 + (.22 * arc if action == 'step_side' else 0))
            y = (.42 * arc if action == 'kick_left' else -.28 * arc if action == 'step_back' else .12 * arc if action == 'foot_tap' else 0)
            height = .25 if action == 'kick_left' else .22 if action == 'stomp_left' else .10
            joints[side + '_Foot'] = [x, y, .08 + height * arc]
        elif action == 'dance':
            sway = _scene_math.sin(seconds * 6 + (0 if side == 'R' else _scene_math.pi))
            joints[side + '_Foot'] = [sign * (.13 + .1 * max(0, sway)), .12 * sway, .08 + .08 * max(0, sway)]
        joints[side + '_Shoulder'] = [sign * .29, 0, 1.48 - crouch]
        hand = [sign * .32, -step * .6, .94 - crouch]
        lift = min(1, arm_u * 3)
        if arm_action == 'speak':
            hand = [sign * (.32 + .025 * _scene_math.sin(arm_seconds * 3 + sign)),
                    .12 * lift, .94 + .25 * lift + .04 * _scene_math.sin(arm_seconds * 4 + sign) - crouch]
        elif arm_action == 'wave_both':
            hand = [sign * (.4 + .12 * _scene_math.sin(arm_seconds * 10)), .08, .94 + lift * .96 - crouch]
        elif arm_action == 'hand_heart':
            hand = [sign * (.32 - .28 * lift), .38 * lift, .94 + .48 * lift - crouch]
        elif arm_action == 'clap':
            spread = .05 + .26 * (.5 + .5 * _scene_math.cos(arm_seconds * 4))
            hand = [sign * (.32 * (1 - lift) + spread * lift), .38 * lift, .94 + .43 * lift - crouch]
        elif arm_action == 'dance':
            hand = [sign * (.32 + .18 * (.5 + .5 * _scene_math.sin(arm_seconds * 6))), .12, 1.15 + .35 * _scene_math.sin(arm_seconds * 6 + sign) - crouch]
        elif arm_action == 'arms_up':
            hand = [sign * (.32 + .12 * lift), .06 * lift, .94 + .96 * lift - crouch]
        elif arm_action in ('push', 'pull'):
            arc = max(0., _scene_math.sin(_scene_math.pi * arm_u))
            reach = .55 * arc if arm_action == 'push' else .55 * (1 - arc) * lift
            hand = [sign * .28, reach, .94 + .44 * lift - crouch]
        if side == 'R':
            if arm_action in ('raise_hand', 'wave'):
                hand = [.4 + (.12 * _scene_math.sin(arm_seconds * 10) if arm_action == 'wave' else 0), .08, .94 + lift * .96 - crouch]
            elif arm_action in ('point', 'reach', 'hold'):
                hand = [.29, lift * .48, .94 + lift * .45 - crouch]
            elif arm_action == 'slap':
                swing = max(0, min(1, (arm_u - .3) / .4))
                hand = [.55 - .9 * swing, .16 + .27 * _scene_math.sin(swing * _scene_math.pi), 1.48 - crouch]
            elif arm_action == 'salute':
                hand = [.32 - .10 * lift, .15 * lift, .94 + .85 * lift - crouch]
            elif arm_action == 'handshake':
                hand = [.32 - .03 * lift, .5 * lift, .94 + .25 * lift + .05 * lift * _scene_math.sin(arm_seconds * 8) - crouch]
        joints[side + '_Hand'] = hand
    return {'joints': joints, 'crouch': crouch,
            'jump': .45 * _scene_math.sin(_scene_math.pi * u) if action == 'jump' else 0,
            'fall': min(1, u * 1.5) if action in ('fall', 'lie_down') else 0}


def camera_transition(index, elapsed, duration, transition):
    """A cut is instantaneous on the boundary, with no interpolation of old bounds."""
    if not index or transition == 'cut':
        return bool(index and elapsed < 1 / 24 - 1e-8 and transition == 'cut'), 1.
    blend = max(0., min(1., elapsed / max(1 / 24, min(.9, duration * .5))))
    return False, blend * blend * (3 - 2 * blend)


def camera_quality(samples, projection_warnings):
    """Aggregate measurable geometry failures; never compare fixed poses across cuts."""
    issues = []
    grouped = {}
    for warning in projection_warnings:
        key = (tuple(warning.get('focus', [])), tuple(warning.get('issues', [])))
        group = grouped.setdefault(key, {'kind': 'framing', 'subjects': list(key[0]),
                                        'reasons': list(key[1]), 'frames': []})
        group['frames'].append(warning['frame'])
    issues.extend(grouped.values())
    anchors, drift = {}, {}
    for sample in samples:
        if sample.get('movement') != 'locked' or sample.get('transitioning'):
            continue
        shot = sample.get('shot', 0)
        anchor = anchors.setdefault(shot, sample)
        displacement = max(sum((a-b)**2 for a,b in zip(sample[k], anchor[k])) ** .5
                           for k in ('position', 'target'))
        if displacement > .02:
            drift[shot] = max(drift.get(shot, 0), displacement)
    issues.extend({'kind': 'locked_drift', 'shot': shot, 'distance': distance} for shot, distance in drift.items())
    return {'status': 'mismatch' if issues else ('matched' if samples else 'unknown'), 'issues': issues,
            'frames': len(samples)}
