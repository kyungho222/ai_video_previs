"""Cloud-only articulated previs helpers. Forward is local +Y; feet use distance-based stance."""

import math as _motion_math


class PrevisRig(dict):
    """Named body parts plus an explicit compatibility API for whole-body root transforms."""

    _root_attributes = frozenset(
        (
            "location",
            "rotation_mode",
            "rotation_euler",
            "rotation_quaternion",
            "scale",
            "animation_data",
            "keyframe_insert",
            "animation_data_clear",
            "name",
        )
    )

    def __getattr__(self, name):
        if name in self._root_attributes:
            return getattr(self["root"], name)
        raise AttributeError(
            "PrevisRig has no " + name + '; access body objects through rig["parts"]'
        )

    def __setattr__(self, name, value):
        if name in self._root_attributes:
            setattr(self["root"], name, value)
            return
        raise AttributeError("Unsupported PrevisRig attribute: " + name)


def _previs_route(points):
    points = [tuple(float(v) for v in p) for p in points]
    if len(points) < 2 or any(len(p) != 3 for p in points):
        raise ValueError("path requires at least two XYZ points")
    if any(p[2] < 0 for p in points):
        raise ValueError("walking path must stay above ground")
    lengths = [_motion_math.dist(a, b) for a, b in zip(points, points[1:])]
    if min(lengths) <= 1e-6:
        raise ValueError("path contains repeated points")
    return points, lengths


def _previs_at(points, lengths, distance):
    remaining = max(0, min(sum(lengths), distance))
    for i, length in enumerate(lengths):
        if remaining <= length or i == len(lengths) - 1:
            a, b = points[i : i + 2]
            t = remaining / length
            heading = _motion_math.atan2(-(b[0] - a[0]), b[1] - a[1])
            return tuple(a[j] + (b[j] - a[j]) * t for j in range(3)), heading
        remaining -= length


def previs_walk_sample(path, time, duration, stride=0.55):
    if duration <= 0 or stride <= 0:
        raise ValueError("duration and stride must be positive")
    points, lengths = _previs_route(path)
    distance = sum(lengths) * max(0, min(1, time / duration))
    position, heading = _previs_at(points, lengths, distance)
    feet = []
    for side, offset in ((-1, 0), (1, 0.5)):
        cycle = distance / (2 * stride) + offset
        n = _motion_math.floor(cycle)
        phase = cycle - n
        planted = (n - offset) * 2 * stride + stride * 0.5
        lift = 0
        if phase < 0.6:
            foot_distance = planted
        else:
            t = (phase - 0.6) / 0.4
            foot_distance = planted + 2 * stride * (t * t * (3 - 2 * t))
            lift = 0.13 * _motion_math.sin(_motion_math.pi * t)
        p, h = _previs_at(points, lengths, foot_distance)
        feet.append(
            (
                p[0] + side * 0.13 * _motion_math.cos(h),
                p[1] + side * 0.13 * _motion_math.sin(h),
                p[2] + 0.08 + lift,
            )
        )
    return {
        "position": position,
        "heading": heading,
        "feet": feet,
        "phase": distance / (2 * stride) * 2 * _motion_math.pi,
    }


def previs_restore_character_limbs(subjects=None):
    """Restore limb visibility for generated characters and older hidden rigs."""
    import bpy
    import re

    if subjects is None:
        subjects = [
            obj
            for obj in bpy.data.objects
            if obj.get("previs_character") or obj.get("previs_motion_version")
        ]
    limb_name = re.compile(
        r"(?:^|[_. -])(?:[lr][_. -]?)?"
        r"(?:upperarm|lowerarm|forearm|upperleg|lowerleg|foreleg|hindleg|thigh|shin|arm|leg|hand|foot|feet|paw|hoof|wing|flipper|knee|elbow|shoulder|wrist|ankle|finger|thumb|toe)"
        r"(?:[_. -]|$)",
        re.I,
    )
    restored = 0
    for subject in subjects:
        root = subject["root"] if isinstance(subject, dict) else subject
        if not (root.get("previs_character") or root.get("previs_motion_version")):
            continue
        for obj in root.children_recursive:
            part = str(obj.get("previs_body_part", "")).lower()
            if part in ("limb", "arm", "leg", "hand", "foot", "paw", "joint") or limb_name.search(
                obj.name
            ):
                obj.hide_render = False
                obj.hide_viewport = False
                restored += 1
    return {"restoredLimbs": restored}


# Compatibility for saved generated code from the head/body-only preview policy.
previs_hide_character_limbs = previs_restore_character_limbs


def previs_humanoid(name="Adult", position=(0, 0, 0), color=(0.2, 0.4, 0.8, 1)):
    import bpy
    from mathutils import Vector

    root = bpy.data.objects.new(name + "_Root", None)
    bpy.context.collection.objects.link(root)
    root.location = position
    root["previs_motion_version"] = "2"
    material = bpy.data.materials.new(name + "_Color")
    material.diffuse_color = color
    parts = {}

    def sphere(label, location, scale):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8)
        obj = bpy.context.object
        obj.name = name + "_" + label
        obj.parent = root
        obj.location = location
        obj.scale = scale
        obj.data.materials.append(material)
        for face in obj.data.polygons:
            face.use_smooth = True
        parts[label] = obj
        return obj

    sphere("Torso", (0, 0, 1.25), (0.25, 0.16, 0.34))
    sphere("Pelvis", (0, 0, 0.93), (0.19, 0.14, 0.14))
    sphere("Head", (0, 0, 1.76), (0.14, 0.13, 0.18))
    # Nose makes facing direction visible.
    sphere("Nose", (0, 0.13, 1.76), (0.045, 0.065, 0.045))
    eye_white = bpy.data.materials.new(name + "_EyeWhite")
    eye_white.diffuse_color = (0.96, 0.97, 1, 1)
    pupil = bpy.data.materials.new(name + "_Pupil")
    pupil.diffuse_color = (0.015, 0.025, 0.04, 1)
    for side, sign in (("L", -1), ("R", 1)):
        eye = sphere(side + "_Eye", (sign * 0.055, 0.119, 1.80), (0.033, 0.019, 0.028))
        eye.data.materials.clear()
        eye.data.materials.append(eye_white)
        dot = sphere(side + "_Pupil", (sign * 0.055, 0.137, 1.80), (0.014, 0.011, 0.018))
        dot.data.materials.clear()
        dot.data.materials.append(pupil)
    for side in ("L", "R"):
        for limb in ("UpperLeg", "LowerLeg", "UpperArm", "LowerArm"):
            bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=1, depth=2)
            obj = bpy.context.object
            obj.name = name + "_" + side + "_" + limb
            obj.parent = root
            obj.data.materials.append(material)
            for face in obj.data.polygons:
                face.use_smooth = True
            parts[side + "_" + limb] = obj
        for joint in ("Knee", "Elbow"):
            sphere(side + "_" + joint, (0, 0, 0), (0.075, 0.075, 0.075))
        sphere(
            side + "_Shoulder", ((-1 if side == "L" else 1) * 0.29, 0, 1.48), (0.075, 0.075, 0.075)
        )
        sphere(side + "_Foot", (0, 0, 0.08), (0.085, 0.17, 0.08))
        sphere(side + "_Hand", (0, 0, 0), (0.065, 0.06, 0.085))
    rig = PrevisRig(root=root, parts=parts)
    previs_walk(rig, [tuple(position), (position[0], position[1] + 0.001, position[2])], 1, 2)
    # Remove the initialization pose keys; the requested animation owns timing.
    for obj in [root, *parts.values()]:
        obj.animation_data_clear()
    previs_restore_character_limbs([root])
    return rig


def previs_walk(rig, path, start_frame=1, end_frame=None, stride=0.55):
    import bpy
    from mathutils import Vector

    end_frame = end_frame or bpy.context.scene.frame_end
    if end_frame <= start_frame:
        raise ValueError("walk frame range must increase")
    _previs_route(path)
    root, parts = rig["root"], rig["parts"]
    duration = (end_frame - start_frame) / bpy.context.scene.render.fps

    def key(obj, frame):
        obj.keyframe_insert(data_path="location", frame=frame)
        obj.keyframe_insert(data_path="rotation_euler", frame=frame)
        obj.keyframe_insert(data_path="scale", frame=frame)

    def segment(label, a, b, radius, frame):
        obj = parts[label]
        delta = b - a
        obj.location = (a + b) / 2
        obj.scale = (radius, radius, delta.length / 2)
        obj.rotation_euler = delta.to_track_quat("Z", "Y").to_euler()
        key(obj, frame)

    for frame in range(start_frame, end_frame + 1):
        s = previs_walk_sample(
            path, (frame - start_frame) / bpy.context.scene.render.fps, duration, stride
        )
        root.location = s["position"]
        root.rotation_euler = (0, 0, s["heading"])
        key(root, frame)
        h = s["heading"]
        c = _motion_math.cos(h)
        sn = _motion_math.sin(h)
        for i, (side, sign) in enumerate((("L", -1), ("R", 1))):
            foot = Vector(s["feet"][i]) - Vector(s["position"])
            foot = Vector((c * foot.x + sn * foot.y, -sn * foot.x + c * foot.y, foot.z))
            hip = Vector((sign * 0.13, 0, 0.93))
            delta = foot - hip
            distance = delta.length
            if distance > 1.0:
                raise ValueError("walk stride exceeds leg reach; reduce stride or path slope")
            bend = Vector((0, 1, 0))
            bend -= delta.normalized() * bend.dot(delta.normalized())
            bend.normalize()
            knee = (hip + foot) / 2 + bend * _motion_math.sqrt(max(0, 0.5**2 - (distance / 2) ** 2))
            segment(side + "_UpperLeg", hip, knee, 0.072, frame)
            segment(side + "_LowerLeg", knee, foot, 0.058, frame)
            parts[side + "_Knee"].location = knee
            key(parts[side + "_Knee"], frame)
            parts[side + "_Foot"].location = foot + Vector((0, 0.04, 0))
            key(parts[side + "_Foot"], frame)
            angle = 0.35 * _motion_math.sin(s["phase"] + i * _motion_math.pi)
            shoulder = Vector((sign * 0.29, 0, 1.48))
            elbow = shoulder + Vector(
                (0, 0.30 * _motion_math.sin(angle), -0.30 * _motion_math.cos(angle))
            )
            hand = elbow + Vector(
                (0, 0.28 * _motion_math.sin(angle + 0.2), -0.28 * _motion_math.cos(angle + 0.2))
            )
            segment(side + "_UpperArm", shoulder, elbow, 0.055, frame)
            segment(side + "_LowerArm", elbow, hand, 0.045, frame)
            parts[side + "_Elbow"].location = elbow
            key(parts[side + "_Elbow"], frame)
            parts[side + "_Hand"].location = hand
            key(parts[side + "_Hand"], frame)
    for obj in [root, *parts.values()]:
        if obj.animation_data:
            for curve in _jutsu_action_curves(obj.animation_data.action):
                for point in curve.keyframe_points:
                    point.interpolation = "LINEAR"
    return {"frames": end_frame - start_frame + 1, "path": path, "motion": "walk", "version": 1}


# Pose sampling stays independent of Blender so anatomical continuity and
# camera/gaze behavior can be regression-tested without a cloud mutation.
def _pv_add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _pv_sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _pv_mul(a, s):
    return tuple(x * s for x in a)


def _pv_dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _pv_unit(a):
    length = _motion_math.sqrt(_pv_dot(a, a))
    if length < 1e-9:
        raise ValueError("zero-length direction")
    return _pv_mul(a, 1 / length)


def _pv_rotate(a, yaw):
    c, s = _motion_math.cos(yaw), _motion_math.sin(yaw)
    return (c * a[0] - s * a[1], s * a[0] + c * a[1], a[2])


def _pv_smooth(t):
    t = max(0, min(1, t))
    return t * t * t * (t * (t * 6 - 15) + 10)


def _pv_mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def _pv_two_bone(start, end, first, second, bend_direction):
    delta = _pv_sub(end, start)
    direction = _pv_unit(delta)
    distance = max(
        abs(first - second) + 1e-6, min(first + second - 1e-6, _motion_math.dist(start, end))
    )
    end = _pv_add(start, _pv_mul(direction, distance))
    bend = _pv_sub(bend_direction, _pv_mul(direction, _pv_dot(bend_direction, direction)))
    if _pv_dot(bend, bend) < 1e-8:
        axis = (1, 0, 0) if abs(direction[0]) < 0.8 else (0, 1, 0)
        bend = _pv_sub(axis, _pv_mul(direction, _pv_dot(axis, direction)))
    bend = _pv_unit(bend)
    along = (first * first - second * second + distance * distance) / (2 * distance)
    height = _motion_math.sqrt(max(0, first * first - along * along))
    return _pv_add(_pv_add(start, _pv_mul(direction, along)), _pv_mul(bend, height)), end


def previs_orbit_sample(
    progress,
    center=(0, 0, 0),
    radius=4.5,
    height=2.1,
    clockwise=True,
    orbit_degrees=360,
    start_angle=0,
):
    if radius <= 0:
        raise ValueError("orbit radius must be positive")
    angle = _motion_math.radians(
        start_angle + orbit_degrees * max(0, min(1, progress)) * (1 if clockwise else -1)
    )
    # Front is +Y. From above, +Y -> +X -> -Y is clockwise.
    return (
        center[0] + radius * _motion_math.sin(angle),
        center[1] + radius * _motion_math.cos(angle),
        center[2] + height,
    )


_PREVIS_POSES = (
    ((-0.08, 0.10, -0.49), (0.20, 0.14, -0.37), 0.93, (-0.04, 0.04)),
    ((-0.18, 0.05, -0.32), (0.07, 0.15, 0.48), 0.88, (0.09, -0.12)),
    ((-0.43, 0.14, 0.20), (0.44, 0.12, 0.10), 0.78, (-0.09, 0.09)),
    ((-0.17, 0.08, 0.49), (0.18, 0.10, 0.47), 0.94, (-0.07, -0.07)),
    ((0.31, 0.24, 0.05), (-0.30, 0.22, 0.14), 0.77, (0.12, 0.11)),
    ((-0.38, 0.14, -0.22), (0.13, 0.14, 0.42), 0.89, (0.05, -0.13)),
    ((-0.24, 0.10, 0.28), (0.22, 0.10, -0.39), 0.93, (-0.04, 0.08)),
)


def _pv_turn_foot(turn, initial_heading, side):
    quarter = _motion_math.pi / 2
    sign = -1 if turn < 0 else 1
    q = abs(turn) / quarter

    def anchor(k):
        width = (0.20, 0.25, 0.18, 0.27, 0.20)[k % 5]
        stagger = (0.04, 0.08, 0.10, 0.05, 0.04)[k % 5]
        yaw = initial_heading + sign * k * quarter
        return (
            _pv_rotate(
                ((-1 if side == 0 else 1) * width, (-1 if side == 0 else 1) * stagger, 0.08), yaw
            ),
            yaw,
        )

    position, yaw = anchor(0)
    for index in range(int(_motion_math.ceil(q))):
        start = index + 0.5 + side * 0.25
        end = start + 0.25
        target, target_yaw = anchor(index + 1)
        if q <= start:
            break
        if q >= end:
            position, yaw = target, target_yaw
            continue
        t = (q - start) / 0.25
        smooth = _pv_smooth(t)
        point = _pv_mix(position, target, smooth)
        return (
            point[0],
            point[1],
            point[2] + 0.12 * _motion_math.sin(_motion_math.pi * t),
        ), yaw + (target_yaw - yaw) * smooth
    return position, yaw


def previs_pose_sample(
    progress,
    position=(0, 0, 0),
    camera_position=None,
    camera_heading=None,
    initial_heading=None,
    energy=1,
    dance_phase=None,
    step_amplitude=0,
):
    u = max(0, min(1, float(progress)))
    energy = max(0.4, min(1.25, float(energy)))
    camera_position = tuple(camera_position or (position[0], position[1] + 4.5, position[2] + 2.1))
    raw_heading = _motion_math.atan2(
        -(camera_position[0] - position[0]), camera_position[1] - position[1]
    )
    camera_heading = raw_heading if camera_heading is None else camera_heading
    initial_heading = raw_heading if initial_heading is None else initial_heading
    turn = camera_heading - initial_heading
    quarter = _motion_math.pi / 2
    q = abs(turn) / quarter
    whole = _motion_math.floor(q)
    fraction = q - whole
    heading = initial_heading + (-1 if turn < 0 else 1) * quarter * (
        whole + _pv_smooth((fraction - 0.5) * 2)
    )
    at = u * (len(_PREVIS_POSES) - 1)
    index = min(len(_PREVIS_POSES) - 2, int(at))
    t = _pv_smooth((at - index - 0.12) / 0.76)
    a, b = _PREVIS_POSES[index : index + 2]
    left, right = _pv_mix(a[0], b[0], t), _pv_mix(a[1], b[1], t)
    pelvis_height = a[2] + (b[2] - a[2]) * t
    lean = _pv_mix(a[3], b[3], t)
    sway = (
        0.04 * _motion_math.sin(4 * _motion_math.pi * u),
        0.025 * _motion_math.sin(2 * _motion_math.pi * u),
        pelvis_height,
    )

    def world(local):
        return _pv_add(position, _pv_rotate(local, heading))

    pelvis = world(sway)
    torso = world(_pv_add(sway, (lean[1] * 0.32, -lean[0] * 0.32, 0.32)))
    head = world(_pv_add(sway, (lean[1] * 0.82, -lean[0] * 0.82, 0.82)))
    joints = {"Pelvis": pelvis, "Torso": torso, "Head": head}
    foot_yaws = {}
    for side, side_index, sign, hand_delta in (("L", 0, -1, left), ("R", 1, 1, right)):
        hip = world(_pv_add(sway, (sign * 0.13, 0, 0)))
        foot, foot_yaw = _pv_turn_foot(turn, initial_heading, side_index)
        if dance_phase is not None and step_amplitude:
            beat = max(0, min(1, dance_phase)) * 8
            cycle = int(beat)
            swing = beat - cycle
            if cycle % 2 == side_index:
                lift = _motion_math.sin(_motion_math.pi * swing) ** 2
                foot = _pv_add(
                    foot,
                    _pv_rotate((sign * step_amplitude * lift, 0.08 * lift, 0.10 * lift), heading),
                )
        foot = _pv_add(position, foot)
        knee, foot = _pv_two_bone(hip, foot, 0.50, 0.50, _pv_rotate((0, 1, 0), heading))
        shoulder = world(_pv_add(sway, (sign * 0.29 + lean[1] * 0.55, -lean[0] * 0.55, 0.55)))
        hand = _pv_add(shoulder, _pv_rotate(_pv_mul(hand_delta, energy), heading))
        elbow, hand = _pv_two_bone(
            shoulder, hand, 0.30, 0.28, _pv_rotate((sign * 0.8, 0.35, -0.12), heading)
        )
        for name, point in (
            ("Hip", hip),
            ("Knee", knee),
            ("Foot", foot),
            ("Shoulder", shoulder),
            ("Elbow", elbow),
            ("Hand", hand),
        ):
            joints[side + "_" + name] = point
        foot_yaws[side] = foot_yaw
    gaze = _pv_unit(_pv_sub(camera_position, head))
    yaw = _motion_math.atan2(-gaze[0], gaze[1])
    relative = (yaw - heading + _motion_math.pi) % (2 * _motion_math.pi) - _motion_math.pi
    pitch = _motion_math.atan2(gaze[2], _motion_math.hypot(gaze[0], gaze[1]))
    return {
        "position": tuple(position),
        "heading": heading,
        "joints": joints,
        "foot_yaws": foot_yaws,
        "torso_euler": (lean[0], lean[1], 0.08 * _motion_math.sin(4 * _motion_math.pi * u)),
        "head_euler": (pitch, 0, relative),
        "gaze_direction": gaze,
        "camera_position": camera_position,
    }


def _pv_key(obj, frame):
    for channel in ("location", "rotation_euler", "scale"):
        obj.keyframe_insert(data_path=channel, frame=frame)


def _pv_linear(objects):
    for obj in objects:
        if obj.animation_data:
            for curve in _jutsu_action_curves(obj.animation_data.action):
                for point in curve.keyframe_points:
                    point.interpolation = "LINEAR"


def _pv_plain_transform(obj, label):
    if getattr(obj, "parent", None) is not None:
        raise ValueError(
            label + " must be unparented; use a plain camera and the original previs_humanoid root"
        )
    if any(abs(value - 1) > 1e-6 for value in getattr(obj, "scale", (1, 1, 1))):
        raise ValueError(label + " must have unit scale; set scene size through helper parameters")
    if any(
        not getattr(c, "mute", False) and getattr(c, "influence", 1) > 0
        for c in getattr(obj, "constraints", [])
    ):
        raise ValueError(
            label
            + " must not have active transform constraints; the previs helper owns its animation"
        )


def previs_orbit_camera(
    camera,
    center=(0, 0, 0),
    radius=4.5,
    height=2.1,
    start_frame=1,
    end_frame=None,
    clockwise=True,
    orbit_degrees=360,
    start_angle=0,
    target_height=1.15,
    focal_length=35,
):
    _pv_plain_transform(camera, "Camera")
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    end_frame = end_frame or scene.frame_end
    if end_frame <= start_frame:
        raise ValueError("orbit frame range must increase")
    if focal_length <= 0:
        raise ValueError("camera focal length must be positive")
    camera.data.lens = focal_length
    camera.rotation_mode = "XYZ"
    previous = None
    for frame in range(start_frame, end_frame + 1):
        u = (frame - start_frame) / (end_frame - start_frame)
        camera.location = previs_orbit_sample(
            u, center, radius, height, clockwise, orbit_degrees, start_angle
        )
        rotation = (
            Vector((center[0], center[1], center[2] + target_height)) - camera.location
        ).to_track_quat("-Z", "Y")
        camera.rotation_euler = (
            rotation.to_euler("XYZ", previous) if previous is not None else rotation.to_euler("XYZ")
        )
        previous = camera.rotation_euler.copy()
        _pv_key(camera, frame)
    _pv_linear([camera])
    scene.camera = camera
    return {"motion": "orbit", "degrees": orbit_degrees, "clockwise": clockwise, "front_axis": "+Y"}


def previs_pose_sequence(
    rig, camera=None, start_frame=1, end_frame=None, energy=1, cycles=1, step_amplitude=0
):
    _pv_plain_transform(rig["root"], "Rig root")
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    end_frame = end_frame or scene.frame_end
    if end_frame <= start_frame:
        raise ValueError("pose frame range must increase")
    root, parts = rig["root"], rig["parts"]
    position = tuple(root.location)
    old_frame = scene.frame_current
    previous_raw = None
    continuous = None
    initial = None

    def segment(label, a, b, radius, frame):
        obj = parts[label]
        delta = b - a
        obj.location = (a + b) / 2
        obj.scale = (radius, radius, delta.length / 2)
        obj.rotation_euler = delta.to_track_quat("Z", "Y").to_euler()
        _pv_key(obj, frame)

    try:
        for frame in range(start_frame, end_frame + 1):
            scene.frame_set(frame)
            camera_position = (
                tuple(camera.matrix_world.translation)
                if camera is not None
                else (position[0], position[1] + 4.5, position[2] + 2.1)
            )
            raw = _motion_math.atan2(
                -(camera_position[0] - position[0]), camera_position[1] - position[1]
            )
            if previous_raw is None:
                continuous = initial = raw
            else:
                continuous += (raw - previous_raw + _motion_math.pi) % (
                    2 * _motion_math.pi
                ) - _motion_math.pi
            previous_raw = raw
            u = (frame - start_frame) / (end_frame - start_frame)
            progress = u
            if cycles != 1:
                progress = (1 - _motion_math.cos(_motion_math.pi * cycles * u)) / 2
                progress = progress + (1 - progress) * _pv_smooth((u - 0.8) / 0.2)
            sample = previs_pose_sample(
                progress,
                position,
                camera_position,
                continuous,
                initial,
                energy,
                dance_phase=u if step_amplitude else None,
                step_amplitude=step_amplitude,
            )
            heading = sample["heading"]
            root.rotation_mode = "XYZ"
            root.location = position
            root.rotation_euler = (0, 0, heading)
            _pv_key(root, frame)

            def local(point):
                return Vector(_pv_rotate(_pv_sub(point, position), -heading))

            joints = {name: local(point) for name, point in sample["joints"].items()}
            for name in ("Pelvis", "Torso", "Head"):
                parts[name].location = joints[name]
                parts[name].rotation_mode = "XYZ"
                parts[name].rotation_euler = (
                    sample["head_euler"]
                    if name == "Head"
                    else (sample["torso_euler"] if name == "Torso" else (0, 0, 0))
                )
                _pv_key(parts[name], frame)
            face_rotation = parts["Head"].rotation_euler.to_matrix()
            for name, offset in [
                ("Nose", (0, 0.13, 0)),
                ("L_Eye", (-0.055, 0.119, 0.04)),
                ("R_Eye", (0.055, 0.119, 0.04)),
                ("L_Pupil", (-0.055, 0.137, 0.04)),
                ("R_Pupil", (0.055, 0.137, 0.04)),
            ]:
                if name in parts:
                    parts[name].location = joints["Head"] + face_rotation @ Vector(offset)
                    parts[name].rotation_euler = sample["head_euler"]
                    _pv_key(parts[name], frame)
            for side in ("L", "R"):
                segment(
                    side + "_UpperLeg", joints[side + "_Hip"], joints[side + "_Knee"], 0.072, frame
                )
                segment(
                    side + "_LowerLeg", joints[side + "_Knee"], joints[side + "_Foot"], 0.058, frame
                )
                segment(
                    side + "_UpperArm",
                    joints[side + "_Shoulder"],
                    joints[side + "_Elbow"],
                    0.055,
                    frame,
                )
                segment(
                    side + "_LowerArm",
                    joints[side + "_Elbow"],
                    joints[side + "_Hand"],
                    0.045,
                    frame,
                )
                for name in ("Knee", "Elbow", "Shoulder", "Hand", "Foot"):
                    key = side + "_" + name
                    if key in parts:
                        parts[key].location = joints[key]
                        if name == "Foot":
                            parts[key].rotation_euler = (0, 0, sample["foot_yaws"][side] - heading)
                        elif name == "Hand":
                            parts[key].rotation_euler = parts[side + "_LowerArm"].rotation_euler
                        _pv_key(parts[key], frame)
        _pv_linear([root, *parts.values()])
        root["previs_articulated_motion"] = "pose_sequence_v2"
    finally:
        scene.frame_set(old_frame)
    return {
        "motion": "pose_sequence",
        "pose_count": len(_PREVIS_POSES),
        "gaze": "camera",
        "frames": end_frame - start_frame + 1,
        "version": 2,
    }


def previs_model_showcase(
    rig,
    camera,
    start_frame=1,
    end_frame=None,
    radius=4.5,
    height=2.1,
    clockwise=True,
    orbit_degrees=360,
    energy=1,
):
    _pv_plain_transform(rig["root"], "Rig root")
    center = tuple(rig["root"].location)
    orbit = previs_orbit_camera(
        camera, center, radius, height, start_frame, end_frame, clockwise, orbit_degrees
    )
    posing = previs_pose_sequence(rig, camera, start_frame, end_frame, energy)
    return {"camera": orbit, "character": posing}


def previs_register_motion(root, markers, *, required_markers=None):
    """Custom humans may register four animated objects/empties following their real joints."""
    import json

    required = ("L_Hand", "R_Hand", "L_Knee", "R_Knee")
    if set(markers) != set(required) or any(
        not getattr(markers[k], "name", None) for k in required
    ):
        raise ValueError("register four animated hand/knee marker objects: " + ", ".join(required))
    action_markers = list(required if required_markers is None else required_markers)
    if (
        not action_markers
        or len(set(action_markers)) != len(action_markers)
        or not set(action_markers) <= set(required)
    ):
        raise ValueError(
            "required_markers must name a nonempty subset of actual hand/knee joints required by the user action"
        )
    root["previs_motion_version"] = "registered"
    root["previs_motion_markers"] = json.dumps({key: markers[key].name for key in required})
    root["previs_required_motion_markers"] = json.dumps(action_markers)


def previs_validate_scene_motion(requirements):
    if not requirements.get("articulated"):
        return {"checked": False}
    import bpy
    import json

    scene = bpy.context.scene
    old_frame = scene.frame_current
    roots = [obj for obj in bpy.data.objects if obj.get("previs_motion_version")]
    best = 0
    passed = 0
    expected = requirements.get("member_count", 1)
    details = []
    if expected > 1 and len(roots) != expected:
        raise RuntimeError(
            "Articulated motion validation failed: expected "
            + str(expected)
            + " human rigs, found "
            + str(len(roots))
        )
    try:
        for root in roots:
            registered = root.get("previs_motion_markers")
            if registered:
                import json

                names = json.loads(registered)
                markers = {
                    name: bpy.data.objects.get(names.get(name, ""))
                    for name in ("L_Hand", "R_Hand", "L_Knee", "R_Knee")
                }
            else:
                markers = {
                    name: next(
                        (obj for obj in root.children if obj.name.endswith("_" + name)), None
                    )
                    for name in ("L_Hand", "R_Hand", "L_Knee", "R_Knee")
                }
            positions = {name: [] for name in markers}
            for index in range(13):
                scene.frame_set(
                    round(scene.frame_start + (scene.frame_end - scene.frame_start) * index / 12)
                )
                for name, obj in markers.items():
                    if obj is not None:
                        point = (
                            root.matrix_world.inverted() @ obj.matrix_world.translation
                            if registered
                            else obj.location
                        )
                        positions[name].append(tuple(point))
            import json

            required_names = json.loads(
                root.get("previs_required_motion_markers", '["L_Hand","R_Hand","L_Knee","R_Knee"]')
            )
            if not required_names or not set(required_names) <= set(markers):
                raise RuntimeError(
                    "Motion validation configuration invalid: select actual joints required by the user action"
                )
            moving_names = [
                name
                for name, points in positions.items()
                if points and max(_motion_math.dist(a, b) for a in points for b in points) > 0.08
            ]
            moving = len(moving_names)
            best = max(best, moving)
            missing = [name for name in required_names if name not in moving_names]
            details.append(
                {
                    "root": root.name,
                    "required": required_names,
                    "moving": moving_names,
                    "missing": missing,
                }
            )
            if not missing:
                passed += 1
                if passed >= expected:
                    return {
                        "checked": True,
                        "moving_joint_markers": moving,
                        "samples": 13,
                        "moving_members": passed,
                    }
    finally:
        scene.frame_set(old_frame)
    raise RuntimeError(
        "Articulated motion validation failed: "
        + json.dumps(details)
        + ". AI must repair the missing real joint animation according to the user action, then render again. If this is a partial-body action, register its genuinely required_markers explicitly; do not invent a dance or disable validation."
    )
