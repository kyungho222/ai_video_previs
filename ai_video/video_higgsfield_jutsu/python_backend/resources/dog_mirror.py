"""Upright dog, exact virtual mirror, and shared marker/writing timeline."""


def previs_mirror_point(point):
    return (point[0], -point[1], point[2])


def _dm_screen_point(point):
    # Camera looks from +Y towards -Y: its screen-right axis is world -X.
    # Reflect the authored layout in X so the foreground dog is on the right
    # and the viewer-facing text reads left-to-right through the aperture.
    return (-point[0], point[1], point[2])


_DM_STROKES = (
    (5.6, 6.4, ((0.50, 0.015, 2.06), (0.485, 0.015, 2.0))),
    (6.65, 7.1, ((0.35, 0.015, 1.94), (0.35, 0.015, 1.80))),
    (7.25, 7.55, ((0.32, 0.015, 1.90), (0.42, 0.015, 1.90))),
    (
        7.75,
        8.65,
        tuple(
            (
                0.47 + 0.035 * _motion_math.cos(2 * _motion_math.pi * i / 16),
                0.015,
                1.845 + 0.045 * _motion_math.sin(2 * _motion_math.pi * i / 16),
            )
            for i in range(17)
        ),
    ),
    (9.0, 9.6, ((1.08, 0.015, 1.94), (1.08, 0.015, 1.84))),
    (9.8, 10.1, ((1.08, 0.015, 1.79), (1.085, 0.015, 1.79))),
)


def _dm_path(points, u):
    lengths = [_motion_math.dist(a, b) for a, b in zip(points, points[1:])]
    total = sum(lengths)
    distance = max(0, min(1, u)) * total
    for i, length in enumerate(lengths):
        if distance <= length or i == len(lengths) - 1:
            return _pv_mix(points[i], points[i + 1], min(1, distance / length))
        distance -= length


def _dm_pulse(t, start, rise, fall, end):
    return _pv_smooth((t - start) / (rise - start)) * (1 - _pv_smooth((t - fall) / (end - fall)))


def _dm_foot(y, side, returning=False):
    initial = 0.38 if returning else 1.95
    direction = 1 if returning else -1
    distance = max(0, min(1.57, (y - initial) * direction))
    stride = 1.57 / 6
    beat = distance / stride + side * 0.5
    k = int(_motion_math.floor(beat + 1e-10))
    phase = beat - k
    u = _pv_smooth((phase - 0.6) / 0.4)
    lift = 4 * u * (1 - u)
    return (
        (-1 if side == 0 else 1) * 0.11,
        initial + direction * (k + u) * stride,
        0.855 + 0.065 * lift,
    )


def previs_dog_mirror_sample(time, duration=15):
    if duration <= 0:
        raise ValueError("dog mirror duration must be positive")
    t = max(0, min(15, float(time) * 15 / duration))
    approach = _pv_smooth((t - 4.1) / 1.1)
    retreat = _pv_smooth((t - 10.5) / 1.3)
    position = (0.6, 1.95 - 1.57 * approach + 1.57 * retreat, 0.8)
    shock = max(_dm_pulse(t, 0.1, 0.7, 1.4, 2.8), _dm_pulse(t, 12, 13, 14.1, 14.9))
    inspect = _dm_pulse(t, 2.8, 3.5, 4.5, 5.2)
    strokes = [_pv_smooth((t - a) / (b - a)) for a, b, _ in _DM_STROKES]
    rest = _pv_add(position, (0.14, -0.12, 0.48))
    nib = (0, -0.10, 0.18)
    pen_target = _pv_add(rest, nib)
    writing = False
    stroke_target = None
    if 5.2 <= t < 5.6:
        pen_target = _pv_mix(pen_target, _DM_STROKES[0][2][0], _pv_smooth((t - 5.2) / 0.4))
    elif 5.6 <= t <= 10.1:
        for i, (a, b, points) in enumerate(_DM_STROKES):
            if a <= t <= b:
                pen_target = _dm_path(points, strokes[i])
                writing = True
                stroke_target = pen_target
                break
            if i + 1 < len(_DM_STROKES) and b < t < _DM_STROKES[i + 1][0]:
                u = _pv_smooth((t - b) / (_DM_STROKES[i + 1][0] - b))
                pen_target = _pv_add(
                    _pv_mix(points[-1], _DM_STROKES[i + 1][2][0], u), (0, 0.08 * 4 * u * (1 - u), 0)
                )
                break
    elif 10.1 < t < 10.5:
        pen_target = _pv_mix(_DM_STROKES[-1][2][-1], pen_target, _pv_smooth((t - 10.1) / 0.4))
    joints = {}
    pelvis = _pv_add(position, (0.012 * _motion_math.sin(t * 1.3), 0, 0.34 - 0.025 * shock))
    joints["Pelvis"] = pelvis
    joints["Torso"] = _pv_add(pelvis, (0, -0.01, 0.20))
    joints["Head"] = _pv_add(position, (0, -0.025, 0.97 + 0.025 * shock))
    for side, index, sign in (("L", 0, -1), ("R", 1, 1)):
        hip = _pv_add(pelvis, (sign * 0.10, 0, 0))
        foot = _dm_foot(position[1], index, t >= 10.5)
        foot = (foot[0] + position[0], foot[1], foot[2])
        knee, foot = _pv_two_bone(hip, foot, 0.18, 0.18, (0, -1, 0))
        shoulder = _pv_add(position, (sign * 0.15, -0.02, 0.76 - 0.015 * shock))
        paw = (
            _pv_add(position, (sign * 0.14, -0.12, 0.48 + 0.025 * shock))
            if side == "L"
            else _pv_sub(pen_target, nib)
        )
        elbow, paw = _pv_two_bone(shoulder, paw, 0.27, 0.27, (sign * 0.6, -0.2, -0.3))
        for name, value in (
            ("Hip", hip),
            ("Knee", knee),
            ("Foot", foot),
            ("Shoulder", shoulder),
            ("Elbow", elbow),
            ("Paw", paw),
        ):
            joints[side + "_" + name] = value
    actual_position = _dm_screen_point(position)
    return {
        "position": actual_position,
        "reflection_position": previs_mirror_point(actual_position),
        "joints": {name: _dm_screen_point(point) for name, point in joints.items()},
        "head_euler": (-0.12 * approach * (1 - retreat) + 0.08 * shock, 0, -0.18 * inspect),
        "mouth_open": shock,
        "ear_back": shock,
        "pen_tip": _dm_screen_point(_pv_add(joints["R_Paw"], nib)),
        "stroke_target": _dm_screen_point(stroke_target) if stroke_target is not None else None,
        "writing": writing,
        "strokes": strokes,
    }


def _dm_sphere(name, scale, material, parent):
    import bpy

    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8)
    obj = bpy.context.object
    obj.name = name
    obj.parent = parent
    obj.scale = scale
    obj.data.materials.append(material)
    return obj


def _dm_dog():
    import bpy

    root = bpy.data.objects.new("MirrorDog_Root", None)
    bpy.context.collection.objects.link(root)
    root["previs_mirror_dog"] = True
    cream = _vt_material("MirrorDog_Cream", (0.83, 0.76, 0.6, 1))
    ear = _vt_material("MirrorDog_Ears", (0.62, 0.43, 0.27, 1))
    black = _vt_material("MirrorDog_Black", (0.012, 0.015, 0.02, 1))
    blue = _vt_material("MirrorDog_Collar", (0.3, 0.55, 0.75, 1))
    red = _vt_material("MirrorDog_Red", (0.85, 0.025, 0.03, 1))
    parts = {}
    for name, scale, mat in (
        ("Pelvis", (0.16, 0.13, 0.18), cream),
        ("Torso", (0.18, 0.14, 0.28), cream),
        ("Head", (0.18, 0.15, 0.17), cream),
        ("Muzzle", (0.095, 0.09, 0.07), cream),
        ("Nose", (0.047, 0.03, 0.035), black),
        ("Mouth", (0.065, 0.025, 0.01), black),
        ("Jaw", (0.08, 0.07, 0.04), cream),
        ("Collar", (0.14, 0.12, 0.025), blue),
    ):
        parts[name] = _dm_sphere(name, scale, mat, root)
    for side in ("L", "R"):
        for name, scale, mat in (
            ("Ear", (0.075, 0.05, 0.15), ear),
            ("Eye", (0.031, 0.022, 0.035), black),
            ("Paw", (0.065, 0.055, 0.065), cream),
            ("Foot", (0.07, 0.09, 0.055), cream),
        ):
            parts[side + "_" + name] = _dm_sphere(side + "_" + name, scale, mat, root)
        for name in ("UpperArm", "LowerArm", "UpperLeg", "LowerLeg"):
            bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=1, depth=2)
            obj = bpy.context.object
            obj.name = side + "_" + name
            obj.parent = root
            obj.data.materials.append(cream)
            parts[side + "_" + name] = obj
    bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=1, depth=2)
    pen = bpy.context.object
    pen.name = "MirrorDog_Pen"
    pen.parent = root
    pen.data.materials.append(red)
    parts["Pen"] = pen
    reflection = bpy.data.objects.new("MirrorDog_Reflection", None)
    bpy.context.collection.objects.link(reflection)
    reflection["previs_mirror_proxy"] = True
    reflection.scale = (1, -1, 1)
    reflected = {}
    for name, obj in parts.items():
        copy = obj.copy()
        copy.data = obj.data
        copy.animation_data_clear()
        bpy.context.collection.objects.link(copy)
        copy.name = "Reflection_" + name
        copy.parent = reflection
        reflected[name] = copy
    return root, parts, reflection, reflected, red


def _dm_bathroom(red):
    import bpy

    before = set(bpy.context.scene.objects)
    wall = _vt_material("Bathroom_Wall", (0.32, 0.30, 0.28, 1))
    counter = _vt_material("Bathroom_Counter", (0.68, 0.68, 0.65, 1))
    frame = _vt_material("Mirror_LED", (0.98, 0.95, 0.84, 1))
    metal = _vt_material("Tap_Metal", (0.45, 0.47, 0.49, 1))
    for name, loc, size in (
        ("Left_Wall", (-1.9, 0, 1.7), (2.2, 0.12, 3.4)),
        ("Right_Wall", (2.3, 0, 1.7), (1.4, 0.12, 3.4)),
        ("Lower_Wall", (0.4, 0, 0.425), (2.4, 0.12, 0.85)),
        ("Upper_Wall", (0.4, 0, 3.0), (2.4, 0.12, 0.9)),
        ("Mirror_Backdrop", (0.4, -2.6, 1.7), (2.4, 0.08, 3.4)),
    ):
        _vt_box(name, loc, size, wall)
    for loc, size in (
        ((-0.8, 0.07, 1.7), (0.04, 0.04, 1.74)),
        ((1.6, 0.07, 1.7), (0.04, 0.04, 1.74)),
        ((0.4, 0.07, 0.85), (2.44, 0.04, 0.04)),
        ((0.4, 0.07, 2.55), (2.44, 0.04, 0.04)),
    ):
        _vt_box("Mirror_Frame", loc, size, frame)
    _vt_box("Vanity_Counter", (0.4, 1.35, 0.71), (2.6, 2.4, 0.18), counter)
    _vt_box("Tap", (-0.35, 0.45, 0.96), (0.08, 0.08, 0.3), metal)
    _vt_box("Tap_Spout", (-0.35, 0.53, 1.08), (0.08, 0.25, 0.06), metal)
    for name, x, color in (
        ("Soap", -0.6, (0.7, 0.65, 0.35, 1)),
        ("Toothbrush", 1.25, (0.2, 0.65, 0.7, 1)),
    ):
        _vt_box(name, (x, 0.4, 0.98), (0.07, 0.07, 0.32), _vt_material(name, color))
    # The glyphs face the viewer, not a second independently generated text image.
    for value, x, z, size in (
        ("I", 0.43, 1.95, 0.15),
        ("m going", 0.54, 1.95, 0.15),
        ("kill you", 0.54, 1.80, 0.13),
    ):
        curve = bpy.data.curves.new("Mirror_Base_Text", "FONT")
        curve.body = value
        curve.size = size
        curve.extrude = 0.001
        obj = bpy.data.objects.new("Mirror_Text", curve)
        bpy.context.collection.objects.link(obj)
        obj.location = (x, -0.015, z)
        obj.rotation_euler.x = _motion_math.pi / 2
        obj.data.materials.append(red)
    strokes = []
    for index, (_, _, points) in enumerate(_DM_STROKES):
        curve = bpy.data.curves.new("Correction_" + str(index), "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.007
        curve.bevel_resolution = 2
        curve.bevel_factor_end = 0
        spline = curve.splines.new("POLY")
        spline.points.add(len(points) - 1)
        for point, co in zip(spline.points, points):
            point.co = (*previs_mirror_point(co), 1)
        obj = bpy.data.objects.new("Correction_" + str(index), curve)
        bpy.context.collection.objects.link(obj)
        curve.materials.append(red)
        strokes.append(obj)
    for obj in bpy.context.scene.objects:
        if obj not in before:
            obj.location.x = -obj.location.x
            obj.scale.x = -obj.scale.x
    return strokes


def previs_dog_mirror_showcase(start_frame=1, end_frame=None, include_text=True):
    import bpy
    import json
    from mathutils import Vector, Euler

    scene = bpy.context.scene
    end_frame = scene.frame_end if end_frame is None else end_frame
    if end_frame <= start_frame:
        raise ValueError("dog mirror frame range must increase")
    if any(o.get("previs_mirror_dog") for o in scene.objects):
        raise ValueError("Only one physical mirror dog is allowed")
    root, parts, reflection, reflected, red = _dm_dog()
    strokes = _dm_bathroom(red)
    root["previs_mirror_pairs"] = json.dumps(
        {obj.name: reflected[name].name for name, obj in parts.items()}
    )
    root["previs_mirror_parts"] = json.dumps({name: obj.name for name, obj in parts.items()})
    root["previs_mirror_pen"] = parts["Pen"].name
    root["previs_mirror_paw"] = parts["R_Paw"].name
    root["previs_mirror_strokes"] = json.dumps([obj.name for obj in strokes])
    root["previs_mirror_text_visible"] = bool(include_text)
    if not include_text:
        for obj in bpy.context.scene.objects:
            if obj in strokes or obj.name.startswith("Mirror_Text"):
                obj.hide_render = True
    bpy.ops.object.camera_add(location=(0, 3.2, 1.65))
    camera = bpy.context.object
    camera.name = "MirrorDog_Fixed_Camera"
    camera.data.lens = 40
    camera.data.sensor_fit = "VERTICAL"
    camera.data.sensor_height = 32
    camera.rotation_euler = (
        (Vector((-0.4, 0, 1.55)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    )
    scene.camera = camera
    offsets = {
        "Muzzle": (0, -0.13, -0.015),
        "Nose": (0, -0.22, 0.015),
        "Mouth": (0, -0.195, -0.08),
        "Jaw": (0, -0.15, -0.12),
        "L_Eye": (-0.065, -0.135, 0.06),
        "R_Eye": (0.065, -0.135, 0.06),
        "L_Ear": (-0.13, 0.01, 0.12),
        "R_Ear": (0.13, 0.01, 0.12),
    }
    for frame in range(start_frame, end_frame + 1):
        s = previs_dog_mirror_sample(15 * (frame - start_frame) / (end_frame - start_frame))
        position = s["position"]
        root.location = position
        reflection.location = s["reflection_position"]
        _pv_key(root, frame)
        _pv_key(reflection, frame)
        local = lambda point: Vector(_pv_sub(point, position))
        for name in ("Pelvis", "Torso", "Head"):
            parts[name].location = local(s["joints"][name])
        parts["Head"].rotation_euler = s["head_euler"]
        rotation = Euler(s["head_euler"]).to_matrix()
        for name, offset in offsets.items():
            off = list(offset)
            if name == "Mouth":
                off[2] -= 0.035 * s["mouth_open"]
                parts[name].scale.z = 0.005 + 0.065 * s["mouth_open"]
            if name == "Jaw":
                off[2] -= 0.09 * s["mouth_open"]
            parts[name].location = local(s["joints"]["Head"]) + rotation @ Vector(off)
            parts[name].rotation_euler = s["head_euler"]
            if name.endswith("Ear"):
                parts[name].rotation_euler.x -= 0.4 * s["ear_back"]
        parts["Collar"].location = local(s["joints"]["Head"]) + Vector((0, 0, -0.18))

        def segment(obj, a, b, radius):
            delta = b - a
            obj.location = (a + b) / 2
            obj.scale = (radius, radius, delta.length / 2)
            obj.rotation_euler = delta.to_track_quat("Z", "Y").to_euler()

        for side in ("L", "R"):
            j = s["joints"]
            for name, a, b, radius in (
                ("UpperArm", "Shoulder", "Elbow", 0.035),
                ("LowerArm", "Elbow", "Paw", 0.03),
                ("UpperLeg", "Hip", "Knee", 0.045),
                ("LowerLeg", "Knee", "Foot", 0.035),
            ):
                segment(
                    parts[side + "_" + name],
                    local(j[side + "_" + a]),
                    local(j[side + "_" + b]),
                    radius,
                )
            for name in ("Paw", "Foot"):
                parts[side + "_" + name].location = local(j[side + "_" + name])
        segment(parts["Pen"], local(s["joints"]["R_Paw"]), local(s["pen_tip"]), 0.018)
        for name, obj in parts.items():
            copy = reflected[name]
            copy.location = obj.location
            copy.rotation_euler = obj.rotation_euler
            copy.scale = obj.scale
            _pv_key(obj, frame)
            _pv_key(copy, frame)
        for obj, value in zip(strokes, s["strokes"]):
            obj.data.bevel_factor_end = value
            obj.data.keyframe_insert(data_path="bevel_factor_end", frame=frame)
    _pv_linear([root, reflection, *parts.values(), *reflected.values()])
    for obj in strokes:
        for curve in _jutsu_action_curves(obj.data.animation_data.action):
            for key in curve.keyframe_points:
                key.interpolation = "LINEAR"
    scene.frame_set(start_frame)
    return {
        "motion": "dog_mirror_grammar",
        "physical_dogs": 1,
        "reflection_proxies": 1,
        "frames": end_frame - start_frame + 1,
        "camera": "fixed",
        "correction_strokes": len(strokes),
        "text_visible": bool(include_text),
        "text_mode": "exact geometry, not model-generated lettering",
    }


def previs_validate_dog_mirror(requirements):
    if not requirements.get("dog_mirror"):
        return {"checked": False}
    import bpy, json

    scene = bpy.context.scene
    old = scene.frame_current
    roots = [o for o in scene.objects if o.get("previs_mirror_dog")]
    proxies = [o for o in scene.objects if o.get("previs_mirror_proxy")]
    if len(roots) != 1 or len(proxies) != 1:
        raise RuntimeError(
            "Dog mirror validation failed: one physical dog and one reflection proxy required"
        )
    if scene.camera is None:
        raise RuntimeError("Dog mirror validation failed: missing fixed camera")
    from mathutils import Matrix, Vector

    root = roots[0]
    pairs = json.loads(root.get("previs_mirror_pairs", "{}"))
    parts = {
        name: bpy.data.objects.get(obj_name)
        for name, obj_name in json.loads(root.get("previs_mirror_parts", "{}")).items()
    }
    if not pairs:
        raise RuntimeError("Dog mirror validation failed: missing mirrored body parts")
    pen = bpy.data.objects.get(root.get("previs_mirror_pen", ""))
    paw = bpy.data.objects.get(root.get("previs_mirror_paw", ""))
    strokes = [
        bpy.data.objects.get(name) for name in json.loads(root.get("previs_mirror_strokes", "[]"))
    ]
    if (
        pen is None
        or paw is None
        or len(strokes) != len(_DM_STROKES)
        or any(o is None for o in strokes)
    ):
        raise RuntimeError("Dog mirror validation failed: missing pen, paw or correction curves")
    if any(
        parts.get(name) is None for name in ("Head", "Mouth", "L_Paw", "R_Paw", "L_Foot", "R_Foot")
    ):
        raise RuntimeError("Dog mirror validation failed: missing articulated face or paws")
    for obj, (_, _, points) in zip(strokes, _DM_STROKES):
        actual_points = obj.data.splines[0].points
        if len(actual_points) != len(points) or any(
            (
                obj.matrix_world @ Vector(actual.co[:3])
                - Vector(previs_mirror_point(_dm_screen_point(expected)))
            ).length
            > 0.005
            for actual, expected in zip(actual_points, points)
        ):
            raise RuntimeError(
                "Dog mirror validation failed: correction geometry moved off the pen path"
            )
    reflect = Matrix(((1, 0, 0, 0), (0, -1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)))
    camera_matrix = None
    contacts = 0
    try:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            sample = previs_dog_mirror_sample(
                15 * (frame - scene.frame_start) / (scene.frame_end - scene.frame_start)
            )
            if (root.matrix_world.translation - Vector(sample["position"])).length > 0.01:
                raise PrevisQualityError("Dog mirror validation failed: approach/retreat motion missing")
            for name in ("Head", "L_Paw", "R_Paw", "L_Foot", "R_Foot"):
                if (
                    parts[name].matrix_world.translation - Vector(sample["joints"][name])
                ).length > 0.015:
                    raise RuntimeError(
                        "Dog mirror validation failed: disconnected or static body joint " + name
                    )
            if abs(parts["Mouth"].scale.z - (0.005 + 0.065 * sample["mouth_open"])) > 0.005:
                raise PrevisQualityError(
                    "Dog mirror validation failed: surprise facial animation missing"
                )
            actual_camera = tuple(v for row in scene.camera.matrix_world for v in row)
            if camera_matrix is None:
                camera_matrix = actual_camera
            if max(abs(a - b) for a, b in zip(actual_camera, camera_matrix)) > 1e-6:
                raise RuntimeError("Dog mirror validation failed: camera must remain fixed")
            for actual_name, mirror_name in pairs.items():
                actual = bpy.data.objects.get(actual_name)
                mirror = bpy.data.objects.get(mirror_name)
                if actual is None or mirror is None:
                    raise RuntimeError("Dog mirror validation failed: missing reflection part")
                expected = reflect @ actual.matrix_world
                if (
                    max(
                        abs(expected[i][j] - mirror.matrix_world[i][j])
                        for i in range(4)
                        for j in range(4)
                    )
                    > 1e-4
                ):
                    raise RuntimeError(
                        "Dog mirror validation failed: reflection drift at " + actual_name
                    )
            tip = pen.matrix_world @ Vector((0, 0, 1))
            grip = pen.matrix_world @ Vector((0, 0, -1))
            if (grip - paw.matrix_world.translation).length > 0.015:
                raise RuntimeError("Dog mirror validation failed: pen detached from paw")
            if sample["writing"]:
                if (tip - Vector(sample["stroke_target"])).length > 0.015:
                    raise RuntimeError(
                        "Dog mirror validation failed: pen tip misses the writing surface"
                    )
                contacts += 1
            for obj, value in zip(strokes, sample["strokes"]):
                if abs(obj.data.bevel_factor_end - value) > 0.015:
                    raise RuntimeError(
                        "Dog mirror validation failed: lettering and pen are out of sync"
                    )
        if contacts < 5:
            raise RuntimeError("Dog mirror validation failed: missing writing contact frames")
        return {
            "checked": True,
            "physical_dogs": 1,
            "reflection_proxies": 1,
            "fixed_camera": True,
            "writing_contact_frames": contacts,
            "correction_strokes": len(strokes),
            "text_visible": bool(root.get("previs_mirror_text_visible")),
        }
    finally:
        scene.frame_set(old)
