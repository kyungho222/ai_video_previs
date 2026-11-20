"""Continuous flying-truck action and an independent camera, in metres/seconds."""


def _vt_curve(time, knots):
    if time <= knots[0][0]:
        return tuple(knots[0][1])
    if time >= knots[-1][0]:
        return tuple(knots[-1][1])
    for (a, p, v), (b, q, w) in zip(knots, knots[1:]):
        if a <= time <= b:
            dt = b - a
            u = (time - a) / dt
            out = []
            for x, y, v0, v1 in zip(p, q, v, w):
                delta = y - x
                start = v0 * dt
                end = v1 * dt
                # Quintic Hermite: shared endpoint velocities, zero endpoint
                # acceleration. Interior waypoints do not force the shot to stop.
                out.append(
                    x
                    + start * u
                    + (10 * delta - 6 * start - 4 * end) * u**3
                    + (-15 * delta + 8 * start + 7 * end) * u**4
                    + (6 * delta - 3 * start - 3 * end) * u**5
                )
            return tuple(out)


def previs_cybertruck_sample(time, duration=15, origin=(0, 0, 0)):
    if duration <= 0:
        raise ValueError("vehicle duration must be positive")
    t = max(0, min(15, float(time) * 15 / duration))
    y = _vt_curve(
        t,
        (
            (0, (0,), (0,)),
            (3, (0,), (0,)),
            (5, (8,), (8,)),
            (9, (66,), (18,)),
            (12, (116,), (14,)),
            (14.2, (143,), (10,)),
            (15, (152,), (12,)),
        ),
    )[0]
    z = _vt_curve(
        t,
        (
            (0, (0,), (0,)),
            (3, (0,), (0,)),
            (5, (15,), (0,)),
            (9, (15,), (0,)),
            (12, (12,), (-1,)),
            (14.2, (0,), (0,)),
            (15, (0,), (0,)),
        ),
    )[0]
    yaw = -2 * _motion_math.pi * _pv_smooth((t - 5) / 4)
    thrust = _pv_smooth((t - 3) / 0.6) * (1 - _pv_smooth((t - 13.6) / 0.6))
    suspension = (
        -0.07 * _motion_math.sin(_motion_math.pi * max(0, min(1, (t - 14.2) / 0.8))) ** 2
        if t >= 14.2
        else 0
    )
    phase = (
        "waiting"
        if t < 3
        else (
            "takeoff"
            if t < 5
            else "spin" if t < 9 else "overflight" if t < 12 else "landing" if t < 14.2 else "drive"
        )
    )
    return {
        "position": _pv_add(origin, (0, y, z)),
        "yaw": yaw,
        "thrust": thrust,
        "suspension": suspension,
        "wheel_angle": -max(0, y - 143) / 0.48,
        "phase": phase,
    }


def previs_cybertruck_camera_sample(time, duration=15, origin=(0, 0, 0)):
    if duration <= 0:
        raise ValueError("camera duration must be positive")
    t = max(0, min(15, float(time) * 15 / duration))
    truck = previs_cybertruck_sample(t, origin=origin)
    if t <= 5:
        offset = _vt_curve(
            t,
            (
                (0, (-2.15, -12, 1.1), (0, 3, 0)),
                (1.5, (-2.15, -4, 1.3), (0, 4, 0.2)),
                (3, (-2.5, 4.4, 1.5), (-0.2, 0, 0.3)),
                (5, (-8, 8, 3.5), (0, 0, 0)),
            ),
        )
    elif t <= 9:
        u = _pv_smooth((t - 5) / 4)
        angle = -_motion_math.pi / 4 - _motion_math.pi * u
        r = _motion_math.sqrt(128)
        offset = (r * _motion_math.sin(angle), r * _motion_math.cos(angle), 3.5 + 2 * u)
    else:
        offset = _vt_curve(
            t,
            (
                (9, (8, -8, 5.5), (0, 0, 0)),
                (10.2, (8, 16, 20), (0, 0, 0)),
                (12, (0, -12, 8), (0, 0, 0)),
                (13.5, (-9, 10, 3), (0, 0, 0)),
                (15, (-8, 12, 2), (0, 0, 0)),
            ),
        )
    target = _pv_add(truck["position"], (0, 0, 1.0))
    if 9 < t < 12:
        reveal = 0.45 * _pv_smooth((t - 9) / 1.5) * (1 - _pv_smooth((t - 10.5) / 1.5))
        target = _pv_mix(target, _pv_add(origin, (0, 80, 1.2)), reveal)
    return {
        "position": _pv_add(truck["position"], offset),
        "target": target,
        "phase": truck["phase"],
    }


def _vt_material(name, color):
    import bpy

    material = bpy.data.materials.new(name)
    material.diffuse_color = color
    return material


def _vt_box(name, location, size, material, parent=None):
    import bpy

    bpy.ops.mesh.primitive_cube_add(size=2)
    obj = bpy.context.object
    obj.name = name
    obj.parent = parent
    obj.location = location
    obj.scale = tuple(v / 2 for v in size)
    obj.data.materials.append(material)
    return obj


def previs_cybertruck(origin=(0, 0, 0)):
    import bpy

    root = bpy.data.objects.new("Cybertruck_Root", None)
    bpy.context.collection.objects.link(root)
    root.location = origin
    root["previs_flying_vehicle"] = True
    root["previs_vehicle_origin"] = list(origin)
    body = bpy.data.objects.new("Cybertruck_Suspension", None)
    bpy.context.collection.objects.link(body)
    body.parent = root
    silver = _vt_material("Cybertruck_Steel", (0.52, 0.56, 0.61, 1))
    black = _vt_material("Cybertruck_Glass", (0.018, 0.035, 0.055, 1))
    rubber = _vt_material("Cybertruck_Tyres", (0.022, 0.025, 0.03, 1))
    cyan = _vt_material("Cybertruck_Thrust", (0.35, 0.85, 1, 1))
    red = _vt_material("Cybertruck_Taillight", (0.95, 0.035, 0.02, 1))
    outline = ((-2.84, 0.6), (2.84, 0.6), (2.84, 1.05), (0.2, 1.85), (-2.84, 1.35))
    vertices = [(x, y, z) for x in (-1.2, 1.2) for y, z in outline]
    faces = [tuple(reversed(range(5))), tuple(range(5, 10))] + [
        (i, (i + 1) % 5, (i + 1) % 5 + 5, i + 5) for i in range(5)
    ]
    mesh = bpy.data.meshes.new("Cybertruck_Wedge")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new("Cybertruck_Body", mesh)
    bpy.context.collection.objects.link(obj)
    obj.parent = body
    obj.data.materials.append(silver)
    glass = _vt_box("Cybertruck_Windshield", (0, 1.24, 1.52), (2.05, 2.05, 0.045), black, body)
    glass.rotation_euler.x = -0.30
    _vt_box("Cybertruck_Front_Light", (0, 2.86, 1.0), (2.18, 0.045, 0.07), cyan, body)
    _vt_box("Cybertruck_Rear_Light", (0, -2.86, 1.24), (2.18, 0.045, 0.07), red, body)
    wheels = []
    effects = []
    for x in (-1.18, 1.18):
        for y in (-1.8, 1.8):
            bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.48, depth=0.3)
            wheel = bpy.context.object
            wheel.name = "Cybertruck_Wheel"
            wheel.parent = root
            wheel.location = (x, y, 0.48)
            wheel.rotation_euler = (0, _motion_math.pi / 2, 0)
            wheel.data.materials.append(rubber)
            wheels.append(wheel)
            bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=0.17, depth=0.24)
            nozzle = bpy.context.object
            nozzle.name = "Cybertruck_Thruster"
            nozzle.parent = root
            nozzle.location = (x * 0.7, y, 0.47)
            nozzle.data.materials.append(black)
            bpy.ops.mesh.primitive_cone_add(vertices=10, radius1=0.3, radius2=0.11, depth=1.5)
            effect = bpy.context.object
            effect.name = "Cybertruck_Jet"
            effect.parent = root
            effect.location = (x * 0.7, y, -0.4)
            effect["previs_visual_effect"] = True
            effect.data.materials.append(cyan)
            effects.append(effect)
    return {"root": root, "body": body, "wheels": wheels, "effects": effects}


def previs_highway(origin=(0, 0, 0)):
    import bpy

    asphalt = _vt_material("Highway_Asphalt", (0.10, 0.12, 0.15, 1))
    white = _vt_material("Highway_White", (0.8, 0.82, 0.75, 1))
    concrete = _vt_material("Highway_Concrete", (0.35, 0.4, 0.43, 1))
    window = _vt_material("Traffic_Glass", (0.04, 0.07, 0.10, 1))
    rubber = _vt_material("Traffic_Tyres", (0.025, 0.026, 0.03, 1))
    brake = _vt_material("Traffic_Brake", (0.9, 0.025, 0.015, 1))
    _vt_box("Highway_Surface", _pv_add(origin, (0, 60, -0.2)), (15, 240, 0.4), asphalt)
    for x in (-7.4, 7.4):
        _vt_box("Highway_Barrier", _pv_add(origin, (x, 60, 0.5)), (0.3, 240, 1), concrete)
    for x in (-2, 2):
        for y in range(-50, 180, 6):
            _vt_box("Highway_Lane", _pv_add(origin, (x, y, 0.012)), (0.12, 3, 0.018), white)
    colors = ((0.7, 0.2, 0.12, 1), (0.18, 0.3, 0.6, 1), (0.55, 0.57, 0.58, 1), (0.8, 0.8, 0.74, 1))
    materials = [_vt_material("Traffic_Color_" + str(i), c) for i, c in enumerate(colors)]

    def car(x, y, index, yaw=0, crash=False):
        root = bpy.data.objects.new("Accident_Car" if crash else "Queued_Car", None)
        bpy.context.collection.objects.link(root)
        root.location = _pv_add(origin, (x, y, 0))
        root.rotation_euler.z = yaw
        root["previs_accident" if crash else "previs_traffic"] = True
        _vt_box("Traffic_Body", (0, 0, 0.65), (1.9, 4.4, 1.0), materials[index % 4], root)
        _vt_box("Traffic_Cabin", (0, -0.1, 1.35), (1.6, 2.4, 0.6), window, root)
        _vt_box("Traffic_Brake_Lights", (0, -2.22, 0.92), (1.6, 0.04, 0.08), brake, root)
        for side in (-0.95, 0.95):
            for axle in (-1.35, 1.35):
                bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=0.34, depth=0.18)
                wheel = bpy.context.object
                wheel.name = "Traffic_Wheel"
                wheel.parent = root
                wheel.location = (side, axle, 0.34)
                wheel.rotation_euler.y = _motion_math.pi / 2
                wheel.data.materials.append(rubber)

    index = 0
    for x in (-4, 0, 4):
        for y in range(-40, 73, 8):
            if x == 0 and y == 0:
                continue
            car(x, y, index)
            index += 1
    car(-1.9, 81, 0, -0.55, True)
    car(1.1, 79, 2, 0.55, True)
    sign = _vt_material("Highway_Sign", (0.03, 0.25, 0.17, 1))
    for x in (-7, 7):
        _vt_box("Sign_Support", _pv_add(origin, (x, 32, 3.3)), (0.2, 0.25, 6.6), concrete)
    _vt_box("Sign_Gantry", _pv_add(origin, (0, 32, 6.5)), (14, 0.3, 0.25), concrete)
    _vt_box("Direction_Sign", _pv_add(origin, (0, 32, 5.8)), (5, 0.15, 1.1), sign)
    # Low-detail skyline beside, never over the flight corridor.
    for i in range(12):
        height = 8 + (i % 5) * 5
        _vt_box(
            "City_Tower",
            _pv_add(origin, (-24 - (i % 3) * 5, i * 16 - 20, height / 2)),
            (5, 7, height),
            concrete,
        )
    return {"queued_cars": index, "accident_y": 80, "landing_y": 143}


def previs_cybertruck_showcase(origin=(0, 0, 0), start_frame=1, end_frame=None):
    import bpy
    from mathutils import Vector, Quaternion, Euler

    scene = bpy.context.scene
    end_frame = scene.frame_end if end_frame is None else end_frame
    if end_frame <= start_frame:
        raise ValueError("vehicle frame range must increase")
    if any(o.get("previs_flying_vehicle") for o in scene.objects):
        raise ValueError("Only one hero Cybertruck is allowed")
    environment = previs_highway(origin)
    rig = previs_cybertruck(origin)
    bpy.ops.object.camera_add()
    camera = bpy.context.object
    camera.name = "Cybertruck_Action_Camera"
    camera.data.lens = 24
    camera.rotation_mode = "XYZ"
    scene.camera = camera
    previous = None
    root = rig["root"]
    root.rotation_mode = "XYZ"
    for frame in range(start_frame, end_frame + 1):
        t = 15 * (frame - start_frame) / (end_frame - start_frame)
        sample = previs_cybertruck_sample(t, origin=origin)
        root.location = sample["position"]
        root.rotation_euler = (0, 0, sample["yaw"])
        _pv_key(root, frame)
        rig["body"].location.z = sample["suspension"]
        _pv_key(rig["body"], frame)
        for wheel in rig["wheels"]:
            wheel.rotation_mode = "QUATERNION"
            wheel.rotation_quaternion = (
                Quaternion((1, 0, 0), sample["wheel_angle"])
                @ Euler((0, _motion_math.pi / 2, 0)).to_quaternion()
            )
            wheel.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        for effect in rig["effects"]:
            effect.scale = (max(0.001, sample["thrust"]),) * 3
            effect.keyframe_insert(data_path="scale", frame=frame)
        view = previs_cybertruck_camera_sample(t, origin=origin)
        camera.location = view["position"]
        rotation = (Vector(view["target"]) - camera.location).to_track_quat("-Z", "Y")
        camera.rotation_euler = (
            rotation.to_euler("XYZ", previous) if previous is not None else rotation.to_euler("XYZ")
        )
        previous = camera.rotation_euler.copy()
        _pv_key(camera, frame)
    _pv_linear([root, rig["body"], camera, *rig["wheels"], *rig["effects"]])
    scene.frame_set(start_frame)
    return {
        "vehicle": "flying_cybertruck",
        "spin_degrees": 360,
        "camera_orbit_degrees": -180,
        "frames": end_frame - start_frame + 1,
        **environment,
    }


def previs_validate_vehicle_action(requirements):
    if not requirements.get("vehicle_flight"):
        return {"checked": False}
    import bpy

    scene = bpy.context.scene
    roots = [o for o in scene.objects if o.get("previs_flying_vehicle")]
    if len(roots) != 1:
        raise RuntimeError("Vehicle validation failed: exactly one hero Cybertruck required")
    from mathutils import Vector

    root = roots[0]
    origin = tuple(root.get("previs_vehicle_origin", (0, 0, 0)))
    old = scene.frame_current
    camera = scene.camera
    if camera is None:
        raise RuntimeError("Vehicle validation failed: no action camera")

    def hero_part(obj):
        while obj is not None:
            if obj == root:
                return True
            obj = obj.parent
        return False

    obstacles = [
        o for o in scene.objects if o.type == "MESH" and not o.hide_render and not hero_part(o)
    ]
    hero_geometry = [
        o
        for o in scene.objects
        if o.type == "MESH" and hero_part(o) and not o.get("previs_visual_effect")
    ]
    if not hero_geometry:
        raise RuntimeError("Vehicle validation failed: missing Cybertruck geometry")
    wheels = [o for o in hero_geometry if o.name.startswith("Cybertruck_Wheel")]
    if len(wheels) != 4:
        raise RuntimeError(
            "Vehicle validation failed: exactly four attached Cybertruck wheels required"
        )
    bounds = [(o.name, *_pv_stage_bounds(o)) for o in obstacles]
    positions = []
    camera_positions = []
    previous = None
    try:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            t = 15 * (frame - scene.frame_start) / (scene.frame_end - scene.frame_start)
            expected = previs_cybertruck_sample(t, origin=origin)
            p = tuple(root.matrix_world.translation)
            cp = tuple(camera.matrix_world.translation)
            if (
                _motion_math.dist(p, expected["position"]) > 0.05
                or abs(root.rotation_euler.z - expected["yaw"]) > 0.02
            ):
                raise RuntimeError(
                    "Vehicle validation failed: wrong flight/spin trajectory at frame " + str(frame)
                )
            if (
                _motion_math.dist(cp, previs_cybertruck_camera_sample(t, origin=origin)["position"])
                > 0.05
            ):
                raise RuntimeError(
                    "Vehicle validation failed: missing independent camera choreography"
                )
            target = previs_cybertruck_camera_sample(t, origin=origin)["target"]
            actual_aim = tuple(camera.matrix_world.to_quaternion() @ Vector((0, 0, -1)))
            if _pv_dot(actual_aim, _pv_unit(_pv_sub(target, cp))) < 0.999:
                raise RuntimeError(
                    "Vehicle validation failed: camera aim does not follow the action/reveal"
                )
            if max(abs(root.rotation_euler.x), abs(root.rotation_euler.y)) > 0.02:
                raise RuntimeError(
                    "Vehicle validation failed: use horizontal yaw spin, not a roll or somersault"
                )
            boxes = [_pv_stage_bounds(o) for o in hero_geometry]
            hero_lo = tuple(min(box[0][i] for box in boxes) for i in range(3))
            hero_hi = tuple(max(box[1][i] for box in boxes) for i in range(3))
            if _pv_segment_box(previous or cp, cp, hero_lo, hero_hi, 0.18):
                raise RuntimeError("Vehicle validation failed: camera intersects the Cybertruck")
            for name, lo, hi in bounds:
                if _pv_segment_box(previous or cp, cp, lo, hi, 0.18):
                    raise RuntimeError(
                        "Vehicle validation failed: camera intersects "
                        + name
                        + " at frame "
                        + str(frame)
                    )
                if hi[2] > origin[2] + 0.05:
                    if all(
                        hero_hi[i] + 0.08 >= lo[i] and hero_lo[i] - 0.08 <= hi[i] for i in range(3)
                    ):
                        raise RuntimeError(
                            "Vehicle validation failed: truck intersects "
                            + name
                            + " at frame "
                            + str(frame)
                        )
            positions.append(p)
            camera_positions.append(cp)
            previous = cp
        if positions[-1][1] <= origin[1] + 100 or abs(positions[-1][2] - origin[2]) > 0.02:
            raise RuntimeError("Vehicle validation failed: landing must be beyond the accident")
        return {
            "checked": True,
            "frames": len(positions),
            "hero_vehicles": 1,
            "wheels": 4,
            "spin_degrees": 360,
            "accident_y": origin[1] + 80,
            "landing_y": origin[1] + 143,
            "obstacles": len(bounds),
            "camera_motion": "independent C2 path",
        }
    finally:
        scene.frame_set(old)
