"""Cloud-only trajectory samples in world meters and scene seconds."""

import math as _object_math


def _previs_object_vector(value):
    result = tuple(float(component) for component in value)
    if len(result) != 3 or not all(_object_math.isfinite(component) for component in result):
        raise ValueError("object motion requires finite XYZ vectors")
    return result


def _previs_object_time(value):
    result = float(value)
    if not _object_math.isfinite(result):
        raise ValueError("object motion requires finite seconds")
    return result


def previs_trajectory_sample(keys, time):
    """Sample cubic Hermite keys with continuous position and velocity.

    keys are dictionaries with time, position and optional velocity in meters/second.
    Endpoint velocities default to zero. Interior velocities use adjacent secants.
    Explicit velocities should connect release, flight and catch phases.
    """
    time = _previs_object_time(time)
    points = [
        (_previs_object_time(key["time"]), _previs_object_vector(key["position"]),
         _previs_object_vector(key["velocity"]) if key.get("velocity") is not None else None)
        for key in keys
    ]
    if len(points) < 2 or any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise ValueError("trajectory requires at least two strictly increasing key times")
    velocities = []
    for index, (stamp, position, velocity) in enumerate(points):
        if velocity is None:
            velocity = (0.0, 0.0, 0.0)
            if 0 < index < len(points) - 1:
                before, after = points[index - 1], points[index + 1]
                left, right = stamp - before[0], after[0] - stamp
                velocity = tuple(
                    ((position[axis] - before[1][axis]) / left * right
                     + (after[1][axis] - position[axis]) / right * left) / (left + right)
                    for axis in range(3)
                )
        velocities.append(velocity)
    if time < points[0][0]:
        return {"position": points[0][1], "velocity": (0.0, 0.0, 0.0)}
    if time > points[-1][0]:
        return {"position": points[-1][1], "velocity": (0.0, 0.0, 0.0)}
    index = next(i for i in range(len(points) - 1) if time <= points[i + 1][0])
    start, end = points[index], points[index + 1]
    duration = end[0] - start[0]
    u = (time - start[0]) / duration
    a, b = velocities[index:index + 2]
    position = tuple(
        (2*u**3 - 3*u**2 + 1)*start[1][axis]
        + (u**3 - 2*u**2 + u)*duration*a[axis]
        + (-2*u**3 + 3*u**2)*end[1][axis]
        + (u**3 - u**2)*duration*b[axis]
        for axis in range(3)
    )
    velocity = tuple(
        (6*u**2 - 6*u)*start[1][axis]/duration
        + (3*u**2 - 4*u + 1)*a[axis]
        + (-6*u**2 + 6*u)*end[1][axis]/duration
        + (3*u**2 - 2*u)*b[axis]
        for axis in range(3)
    )
    return {"position": position, "velocity": velocity}


def previs_ballistic_sample(start, velocity, time, gravity=(0.0, 0.0, -9.81)):
    """Unconstrained flight since release. Caller changes phase at contact."""
    start, velocity, gravity = map(_previs_object_vector, (start, velocity, gravity))
    time = max(0.0, _previs_object_time(time))
    return {
        "position": tuple(start[i] + velocity[i]*time + 0.5*gravity[i]*time*time
                          for i in range(3)),
        "velocity": tuple(velocity[i] + gravity[i]*time for i in range(3)),
    }


def previs_ballistic_between(start, end, duration, time, gravity=(0.0, 0.0, -9.81)):
    """Flight reaching end after duration seconds; time is local to this phase.

    The returned velocity supplies the release/catch boundary conditions.
    This helper samples flight only and does not model a catch or impact.
    """
    start, end, gravity = map(_previs_object_vector, (start, end, gravity))
    duration, time = map(_previs_object_time, (duration, time))
    if duration <= 0:
        raise ValueError("flight duration must be positive")
    velocity = tuple((end[i]-start[i])/duration - 0.5*gravity[i]*duration
                     for i in range(3))
    return previs_ballistic_sample(start, velocity, min(duration, max(0, time)), gravity)


def previs_bounce_sample(start, velocity, time, radius=0.12, floor=0.0,
                         restitution=0.7, gravity=9.81):
    """Sphere flight and passive vertical rebounds against a horizontal floor.

    Horizontal velocity remains constant. No friction, obstacles or external
    hand impulses are simulated. Start a new phase when a hand pushes the ball.
    """
    start, velocity = map(_previs_object_vector, (start, velocity))
    time = max(0.0, _previs_object_time(time))
    radius, floor, restitution, gravity = map(_previs_object_time,
                                              (radius, floor, restitution, gravity))
    if radius <= 0 or gravity <= 0 or not 0 <= restitution < 1:
        raise ValueError("bounce requires radius/gravity > 0 and 0 <= restitution < 1")
    ground = floor + radius
    height = start[2] - ground
    if height < -1e-8:
        raise ValueError("sphere starts below its floor contact height")
    impact_speed = _object_math.sqrt(velocity[2]**2 + 2*gravity*max(0, height))
    impact_time = (velocity[2] + impact_speed)/gravity
    if time < impact_time:
        result = previs_ballistic_sample(start, velocity, time, (0, 0, -gravity))
        result["contact"] = False
        return result
    elapsed = time - impact_time
    upward = restitution*impact_speed
    period = 2*upward/gravity
    settle_time = period/(1-restitution)
    z, vertical = ground, 0.0
    if restitution > 0 and period > 0 and elapsed < settle_time:
        count = max(0, _object_math.floor(
            _object_math.log1p(-elapsed/settle_time)/_object_math.log(restitution)))
        decay = restitution**count
        since_contact = elapsed - period*(1-decay)/(1-restitution)
        since_contact = max(0.0, min(period*decay, since_contact))
        vertical = upward*decay - gravity*since_contact
        z = max(ground, ground + upward*decay*since_contact
                - 0.5*gravity*since_contact**2)
    return {
        "position": (start[0] + velocity[0]*time, start[1] + velocity[1]*time, z),
        "velocity": (velocity[0], velocity[1], vertical),
        "contact": z <= ground + 1e-8,
    }


def previs_motion_blend(first, second, time, start, end):
    """Blend two samples evaluated at the SAME time using a quintic transition.

    Includes the derivative of the blend weight in the resulting velocity.
    Use flight without blending across a deliberate hard collision.
    """
    time, start, end = map(_previs_object_time, (time, start, end))
    if end <= start:
        raise ValueError("motion blend end must follow start")
    a, b = (_previs_object_vector(sample["position"]) for sample in (first, second))
    va, vb = (_previs_object_vector(sample["velocity"]) for sample in (first, second))
    u = max(0.0, min(1.0, (time-start)/(end-start)))
    weight = 6*u**5 - 15*u**4 + 10*u**3
    rate = 30*u*u*(u-1)**2/(end-start)
    return {
        "position": tuple((1-weight)*a[i] + weight*b[i] for i in range(3)),
        "velocity": tuple((1-weight)*va[i] + weight*vb[i] + rate*(b[i]-a[i])
                          for i in range(3)),
    }


def previs_roll_rotation(distance, radius, axis=(0.0, 1.0, 0.0), initial_angle=0.0):
    """Return a WXYZ quaternion for signed travel on a straight, fixed roll axis.

    Use separate rigid orientation handling for changing surface normals/turns.
    """
    distance, radius, initial_angle = map(_previs_object_time, (distance, radius, initial_angle))
    axis = _previs_object_vector(axis)
    length = _object_math.sqrt(sum(value*value for value in axis))
    if radius <= 0 or length <= 1e-12:
        raise ValueError("rolling requires a positive radius and nonzero axis")
    half_angle = (initial_angle + distance/radius)/2
    scale = _object_math.sin(half_angle)/length
    return (_object_math.cos(half_angle), *(value*scale for value in axis))


def previs_footplant_sample(initial, steps, time, lift=0.12):
    """World-space foot contact with explicit swing intervals.

    Each step is {start, end, position, lift?}; position is the destination
    contact point, using the same sole/ankle convention as initial. Between
    steps the contact point stays fixed regardless of root motion. Caller
    plans feasible steps and a separate airborne pose during a jump.
    """
    position = _previs_object_vector(initial)
    time, lift = map(_previs_object_time, (time, lift))
    if lift < 0:
        raise ValueError("foot lift must be nonnegative")
    schedule = []
    previous_end = None
    for step in steps:
        start, end = map(_previs_object_time, (step["start"], step["end"]))
        target = _previs_object_vector(step["position"])
        height = _previs_object_time(step.get("lift", lift))
        if end <= start or height < 0 or (previous_end is not None and start < previous_end):
            raise ValueError("foot steps require ordered nonoverlapping swing intervals")
        schedule.append((start, end, target, height))
        previous_end = end
    for start, end, target, height in schedule:
        if time < start:
            break
        if time < end:
            duration = end - start
            u = (time - start) / duration
            weight = 6 * u**5 - 15 * u**4 + 10 * u**3
            rate = 30 * u**2 * (u - 1)**2 / duration
            arc = 16 * height * u**2 * (1 - u)**2
            arc_rate = 32 * height * u * (1 - u) * (1 - 2 * u) / duration
            return {
                "position": tuple(
                    position[i] + weight * (target[i] - position[i])
                    + (arc if i == 2 else 0.0)
                    for i in range(3)
                ),
                "velocity": tuple(
                    rate * (target[i] - position[i]) + (arc_rate if i == 2 else 0.0)
                    for i in range(3)
                ),
                "contact": False,
            }
        position = target
    return {"position": position, "velocity": (0.0, 0.0, 0.0), "contact": True}


def previs_jump_sample(time, takeoff, landing, start_height=0.0, end_height=None, gravity=9.81):
    """Vertical ballistic flight with an explicit takeoff and landing event.

    Height is the root's world Z or a consistently used vertical offset.
    Duration determines launch speed and apex. Preparation and landing
    absorption belong to grounded character posing outside this flight.
    """
    if end_height is None:
        end_height = start_height
    time, takeoff, landing, start_height, end_height, gravity = map(
        _previs_object_time, (time, takeoff, landing, start_height, end_height, gravity)
    )
    if landing <= takeoff or gravity <= 0:
        raise ValueError("jump requires landing after takeoff and positive gravity")
    duration = landing - takeoff
    launch = (end_height - start_height) / duration + 0.5 * gravity * duration
    impact = launch - gravity * duration
    if time < takeoff:
        height, velocity, phase = start_height, 0.0, "grounded"
    elif time < landing:
        elapsed = time - takeoff
        height = start_height + launch * elapsed - 0.5 * gravity * elapsed**2
        velocity, phase = launch - gravity * elapsed, "flight"
    else:
        height, velocity, phase = end_height, 0.0, "landed"
    return {
        "height": height, "velocity": velocity, "phase": phase,
        "takeoff_velocity": launch, "landing_velocity": impact,
    }


def previs_support_height(pelvis, supports, leg_length):
    """Lower pelvis Z as needed to keep planted feet within leg reach.

    supports contains {position, hip_offset} in world meters. hip_offset is
    the pelvis-to-hip vector already rotated into world axes. Horizontal
    overreach returns reachable=False so the caller can change the root
    route or step placement without moving a planted foot to hide it.
    """
    pelvis = _previs_object_vector(pelvis)
    leg_length = _previs_object_time(leg_length)
    if leg_length <= 0:
        raise ValueError("leg length must be positive")
    upper = pelvis[2]
    lower = -float("inf")
    reachable = True
    reach = leg_length * (1 - 1e-4)
    for support in supports:
        foot = _previs_object_vector(support["position"])
        offset = _previs_object_vector(support["hip_offset"])
        horizontal_squared = sum((pelvis[i] + offset[i] - foot[i])**2 for i in (0, 1))
        if horizontal_squared >= reach**2:
            reachable = False
            continue
        vertical = _object_math.sqrt(reach**2 - horizontal_squared)
        lower = max(lower, foot[2] - offset[2])
        upper = min(upper, foot[2] + vertical - offset[2])
    if upper < lower:
        reachable = False
    return {"position": (pelvis[0], pelvis[1], upper), "reachable": reachable}


def previs_grip_position(hand, offset=(0.0, 0.0, 0.0)):
    """Current evaluated hand/grip position; offset is hand-local METERS.

    Evaluate after character animation is keyed and the scene frame is set.
    Hand mesh scale is excluded so a small proxy hand does not shrink the
    grip offset. Use this final IK position when attaching a rigid prop.
    """
    import bpy
    from mathutils import Vector

    offset = _previs_object_vector(offset)
    graph = bpy.context.evaluated_depsgraph_get()
    matrix = hand.evaluated_get(graph).matrix_world
    position = matrix.translation + matrix.to_quaternion() @ Vector(offset)
    return tuple(float(value) for value in position)
