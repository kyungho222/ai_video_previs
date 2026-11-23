"""Pure Python animation sampling; the same code runs locally and in Blender."""
import math as _scene_math


def _scene_mix(a, b, u):
    return [x + (y - x) * u for x, y in zip(a, b)]


def sample_track(plan, subject_id, time):
    subject = next(s for s in plan['subjects'] if s['id'] == subject_id)
    position, yaw = subject['position'][:], subject['yaw']
    last_action, last_target = 'idle', None
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
                    'phase': u, 'seconds': time - track['start'], 'target': track['target']}
        position, yaw = track['position'][:], track['yaw']
        last_action, last_target = track['action'], track['target']
    # Pose states persist between action intervals; cyclic gestures stop at the endpoint.
    action = last_action if last_action in ('sit', 'crouch', 'fall', 'lie_down', 'hold', 'raise_hand', 'point', 'reach') else 'idle'
    return {'position': position, 'yaw': yaw, 'action': action, 'phase': 1., 'seconds': 0., 'target': last_target}


def human_pose(sample):
    """Return connected joint targets in local metres; shoulders/hips never detach."""
    action, u, seconds = sample['action'], sample['phase'], sample['seconds']
    gait = _scene_math.sin(seconds * (13 if action == 'run' else 8)) if action in ('walk', 'run') else 0
    crouch = .42 * (u if action in ('sit', 'crouch') else 0)
    joints = {}
    for side, sign in (('L', -1), ('R', 1)):
        step = gait * sign * (.27 if action == 'run' else .16)
        joints[side + '_Hip'] = [sign * .13, 0, .93 - crouch]
        joints[side + '_Foot'] = [sign * .13, step, .08 + max(0, step) * .6]
        joints[side + '_Shoulder'] = [sign * .29, 0, 1.48 - crouch]
        hand = [sign * .32, -step * .6, .94 - crouch]
        if side == 'R':
            lift = min(1, u * 3)
            if action in ('raise_hand', 'wave'):
                hand = [.4 + (.12 * _scene_math.sin(seconds * 10) if action == 'wave' else 0), .08, .94 + lift * .96 - crouch]
            elif action in ('point', 'reach', 'hold'):
                hand = [.29, lift * .48, .94 + lift * .45 - crouch]
            elif action == 'slap':
                swing = max(0, min(1, (u - .3) / .4))
                hand = [.55 - .9 * swing, .16 + .27 * _scene_math.sin(swing * _scene_math.pi), 1.48 - crouch]
        joints[side + '_Hand'] = hand
    return {'joints': joints, 'crouch': crouch,
            'jump': .45 * _scene_math.sin(_scene_math.pi * u) if action == 'jump' else 0,
            'fall': min(1, u * 1.5) if action in ('fall', 'lie_down') else 0}
