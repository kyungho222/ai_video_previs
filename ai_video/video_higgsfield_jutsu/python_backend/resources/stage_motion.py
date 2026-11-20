"""Camera-first group previs: explicit flight geometry, not lens-only effects."""


def previs_camera_curve_sample(keys, t):
    """C1 Hermite interpolation; keys are (normalized time, xyz) pairs."""
    if len(keys) < 2:
        raise ValueError("camera curve requires at least two keys")
    times = [float(key[0]) for key in keys]
    points = [tuple(float(value) for value in key[1]) for key in keys]
    if (
        any(len(point) != 3 for point in points)
        or any(not _motion_math.isfinite(v) for p in points for v in p)
        or any(not _motion_math.isfinite(v) for v in times)
        or any(b <= a for a, b in zip(times, times[1:]))
    ):
        raise ValueError("camera keys must have increasing finite times and 3D points")
    t = max(times[0], min(times[-1], float(t)))
    if t == times[-1]:
        return points[-1]
    i = next(i for i in range(len(times) - 1) if times[i] <= t <= times[i + 1])
    span = times[i + 1] - times[i]
    u = (t - times[i]) / span

    def velocity(j, axis):
        if j == 0 or j == len(times) - 1:
            return 0.0
        return (points[j + 1][axis] - points[j - 1][axis]) / (times[j + 1] - times[j - 1])

    return tuple(
        (2 * u**3 - 3 * u**2 + 1) * points[i][axis]
        + (u**3 - 2 * u**2 + u) * span * velocity(i, axis)
        + (-2 * u**3 + 3 * u**2) * points[i + 1][axis]
        + (u**3 - u**2) * span * velocity(i + 1, axis)
        for axis in range(3)
    )


def previs_camera_register_subjects(camera, subjects):
    import json

    camera["previs_framing_subjects"] = json.dumps([obj.name for obj in subjects])


def previs_camera_smooth(
    camera, positions, targets, subjects=None, start_frame=1, end_frame=None, focal_length=None
):
    """Animate only a camera; scene subjects are never modified."""
    import bpy
    from mathutils import Vector

    _pv_plain_transform(camera, "Camera")
    scene = bpy.context.scene
    end_frame = scene.frame_end if end_frame is None else end_frame
    if end_frame <= start_frame:
        raise ValueError("invalid camera frame range")
    if positions[0][0] != 0 or positions[-1][0] != 1 or targets[0][0] != 0 or targets[-1][0] != 1:
        raise ValueError("camera position and target keys must span 0 to 1")
    if focal_length is not None:
        if focal_length <= 0:
            raise ValueError("invalid lens")
        camera.data.lens = focal_length
    if subjects:
        previs_camera_register_subjects(camera, subjects)
    camera.rotation_mode = "XYZ"
    previous = None
    for frame in range(start_frame, end_frame + 1):
        t = (frame - start_frame) / (end_frame - start_frame)
        position = Vector(previs_camera_curve_sample(positions, t))
        target = Vector(previs_camera_curve_sample(targets, t))
        if (target - position).length < 1e-6:
            raise ValueError("camera target equals position")
        camera.location = position
        rotation = (target - position).to_track_quat("-Z", "Y")
        camera.rotation_euler = (
            rotation.to_euler("XYZ", previous) if previous is not None else rotation.to_euler("XYZ")
        )
        previous = camera.rotation_euler.copy()
        _pv_key(camera, frame)
    _pv_linear([camera])
    scene.camera = camera
    return {
        "motion": "smooth_camera",
        "frames": end_frame - start_frame + 1,
        "subjects": len(subjects or []),
    }


def previs_validate_camera(requirements):
    """Measure camera movement; require complete framing only when explicitly requested."""
    import bpy
    import json
    from mathutils import Vector
    from bpy_extras.object_utils import world_to_camera_view

    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        raise RuntimeError("Camera validation failed: no scene camera")
    names = json.loads(camera.get("previs_framing_subjects", "[]"))
    if requirements.get("full_subject_in_frame") and not names:
        raise RuntimeError(
            "Camera framing validation failed: register requested subjects with previs_camera_smooth(subjects=[...])"
        )
    roots = [scene.objects.get(name) for name in names]
    if any(root is None for root in roots):
        raise RuntimeError("Camera framing validation failed: subject missing")

    def belongs(obj):
        while obj is not None:
            if obj in roots:
                return True
            obj = obj.parent
        return False

    meshes = [
        obj for obj in scene.objects if obj.type == "MESH" and not obj.hide_render and belongs(obj)
    ]
    if requirements.get("full_subject_in_frame") and not meshes:
        raise RuntimeError("Camera framing validation failed: no visible subject meshes")
    old = scene.frame_current
    positions = []
    directions = []
    first_outside = None
    try:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            positions.append(tuple(camera.matrix_world.translation))
            directions.append(tuple(camera.matrix_world.to_quaternion() @ Vector((0, 0, -1))))
            if requirements.get("full_subject_in_frame"):
                depsgraph = bpy.context.evaluated_depsgraph_get()
                for obj in meshes:
                    evaluated = obj.evaluated_get(depsgraph)
                    for corner in evaluated.bound_box:
                        view = world_to_camera_view(
                            scene, camera, evaluated.matrix_world @ Vector(corner)
                        )
                        if (
                            view.z <= 0
                            or view.x < 0.01
                            or view.x > 0.99
                            or view.y < 0.01
                            or view.y > 0.99
                        ):
                            first_outside = (frame, obj.name)
                            break
                    if first_outside:
                        break
                if first_outside:
                    break
        if first_outside:
            raise PrevisQualityError(
                "Camera framing validation failed: subject outside frame at frame "
                + str(first_outside[0])
                + ": "
                + first_outside[1]
            )
        steps = [_motion_math.dist(a, b) for a, b in zip(positions, positions[1:])]
        turns = [_motion_math.dist(a, b) for a, b in zip(directions, directions[1:])]
        return {
            "checked": True,
            "frames": len(positions),
            "framingChecked": bool(requirements.get("full_subject_in_frame")),
            "maxFrameTranslation": max(steps, default=0),
            "maxDirectionDelta": max(turns, default=0),
        }
    finally:
        scene.frame_set(old)


def previs_group_positions(center=(0, 0, 0)):
    return [
        _pv_add(center, p)
        for p in ((0, 0, 0), (-2.4, -0.8, 0), (2.4, -0.8, 0), (-5, -2.4, 0), (5, -2.4, 0))
    ]


def previs_stage_camera_sample(time, duration=10, center=(0, 0, 0)):
    if duration <= 0:
        raise ValueError("camera duration must be positive")
    t = max(0, min(10, float(time) * 10 / duration))
    target = (0, 0, 1.1)
    if t < 2:
        u = t / 2
        s = _pv_smooth(u)
        position = _pv_mix((0, 10, 2.8), (0, 4, 1.6), s)
        # Oscillating aim is a real pan; envelope makes aim/velocity continuous.
        target = (
            1.8
            * _motion_math.sin(4 * _motion_math.pi * u)
            * _motion_math.sin(_motion_math.pi * u) ** 2,
            0,
            1.1,
        )
        phase = "dolly_whip"
    elif t < 4:
        s = _pv_smooth((t - 2) / 2)
        angle = _motion_math.pi * s
        position = (4 * _motion_math.sin(angle), 4 * _motion_math.cos(angle), 1.6 - 0.95 * s)
        phase = "orbit_low"
    elif t < 6:
        position = _pv_mix((0, -4, 0.65), (0, -0.3, 9), _pv_smooth((t - 4) / 2))
        phase = "crane_top"
    elif t < 8:
        knots = (
            (6, (0, -0.3, 9)),
            (6.5, (1.2, -4, 4)),
            (7, (1.2, -2.5, 1.4)),
            (7.6, (1.2, 2.8, 1.4)),
            (8, (0, 8, 2)),
        )
        for (a, p), (b, q) in zip(knots, knots[1:]):
            if a <= t <= b:
                position = _pv_mix(p, q, _pv_smooth((t - a) / (b - a)))
                break
        # Gaze stays attached to the group; never flip a forward-facing drone
        # camera 180 degrees in a single frame at the end of a fly-through.
        phase = "dive_flythrough"
    else:
        s = _pv_smooth((t - 8) / 2)
        angle = 2 * _motion_math.pi * s
        position = (8 * _motion_math.sin(angle), 8 * _motion_math.cos(angle), 2 - 0.9 * s)
        phase = "orbit_hero"
    return {
        "position": _pv_add(center, position),
        "target": _pv_add(center, target),
        "phase": phase,
    }


def previs_stage_camera(camera, center=(0, 0, 0), start_frame=1, end_frame=None, focal_length=24):
    if not globals().get("_jutsu_motion_requirements", {}).get("camera_choreography"):
        raise ValueError("The FPV camera requires an explicit FPV choreography request")
    import bpy
    from mathutils import Vector

    _pv_plain_transform(camera, "Camera")
    scene = bpy.context.scene
    end_frame = scene.frame_end if end_frame is None else end_frame
    if end_frame <= start_frame or focal_length <= 0:
        raise ValueError("invalid stage camera frame range/lens")
    camera.data.lens = focal_length
    camera.rotation_mode = "XYZ"
    previous = None
    for frame in range(start_frame, end_frame + 1):
        sample = previs_stage_camera_sample(
            10 * (frame - start_frame) / (end_frame - start_frame), center=center
        )
        camera.location = sample["position"]
        rotation = (Vector(sample["target"]) - camera.location).to_track_quat("-Z", "Y")
        camera.rotation_euler = (
            rotation.to_euler("XYZ", previous) if previous is not None else rotation.to_euler("XYZ")
        )
        previous = camera.rotation_euler.copy()
        _pv_key(camera, frame)
    _pv_linear([camera])
    scene.camera = camera
    camera["previs_stage_camera"] = True
    return {
        "motion": "group_fpv",
        "phases": 5,
        "frames": end_frame - start_frame + 1,
        "focal_length": focal_length,
    }


def previs_dancer_group(center=(0, 0, 0), start_frame=1, end_frame=None):
    """Create five dancers without changing the camera."""
    import bpy

    scene = bpy.context.scene
    end_frame = scene.frame_end if end_frame is None else end_frame
    colors = (
        (0.15, 0.4, 0.85, 1),
        (0.85, 0.2, 0.3, 1),
        (0.2, 0.75, 0.6, 1),
        (0.7, 0.3, 0.85, 1),
        (0.95, 0.65, 0.15, 1),
    )
    rigs = [
        previs_humanoid("Idol_" + str(i + 1), position=p, color=colors[i])
        for i, p in enumerate(previs_group_positions(center))
    ]
    for i, rig in enumerate(rigs):
        previs_pose_sequence(
            rig, None, start_frame, end_frame, energy=1.05, cycles=2, step_amplitude=0.22
        )
        rig["root"]["previs_group_member"] = i + 1
    return rigs


def previs_group_showcase(camera, center=(0, 0, 0), start_frame=1, end_frame=None):
    """Legacy combined helper: explicitly selected FPV only."""
    if not globals().get("_jutsu_motion_requirements", {}).get("camera_choreography"):
        raise ValueError("The FPV group camera requires an explicit FPV choreography request")
    rigs = previs_dancer_group(center, start_frame, end_frame)
    camera_report = previs_stage_camera(camera, center, start_frame, end_frame)
    clearance = previs_clear_stage_path(camera, center=center)
    return {
        "camera": camera_report,
        "members": 5,
        "formation": "wide V with reserved x=1.2 flight lane",
        "stageClearance": clearance,
    }


def _pv_segment_box(a, b, lower, upper, margin=0.15):
    near, far = 0.0, 1.0
    for axis in range(3):
        delta = b[axis] - a[axis]
        lo = lower[axis] - margin
        hi = upper[axis] + margin
        if abs(delta) < 1e-10:
            if a[axis] < lo or a[axis] > hi:
                return False
        else:
            t0, t1 = sorted(((lo - a[axis]) / delta, (hi - a[axis]) / delta))
            near = max(near, t0)
            far = min(far, t1)
            if near > far:
                return False
    return True


def previs_stage_obstacle_offset(lower, upper, path, center=(0, 0, 0), margin=0.35):
    """Smallest bounded outward XY shift clearing the complete camera sweep."""
    if len(path) < 2:
        raise ValueError("camera path must contain at least two points")

    def intersects(delta):
        lo = _pv_add(lower, delta)
        hi = _pv_add(upper, delta)
        return any(_pv_segment_box(a, b, lo, hi, margin) for a, b in zip(path, path[1:]))

    if not intersects((0, 0, 0)):
        return (0, 0, 0)
    x = (lower[0] + upper[0]) / 2 - center[0]
    y = (lower[1] + upper[1]) / 2 - center[1]
    length = _motion_math.hypot(x, y)
    direction = (x / length, y / length, 0) if length > 1e-6 else (0, -1, 0)
    for step in range(1, 65):
        delta = _pv_mul(direction, step * 0.5)
        if not intersects(delta):
            return delta
    raise RuntimeError("Stage clearance failed: cannot relocate obstacle within 32 metres")


def _pv_stage_obstacles(scene):
    def human_part(obj):
        while obj is not None:
            if obj.get("previs_motion_version"):
                return True
            obj = obj.parent
        return False

    return [
        o for o in scene.objects if o.type == "MESH" and not o.hide_render and not human_part(o)
    ]


def _pv_stage_bounds(obj):
    from mathutils import Vector

    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    return tuple(min(c[i] for c in corners) for i in range(3)), tuple(
        max(c[i] for c in corners) for i in range(3)
    )


def previs_clear_stage_path(camera=None, center=(0, 0, 0)):
    """Camera-first layout: move only intersecting static decorations, never rigs/floor/camera."""
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    camera = camera or scene.camera
    if camera is None:
        raise RuntimeError("Stage clearance failed: no scene camera")
    old = scene.frame_current
    path = []
    plans = []
    try:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            path.append(tuple(camera.matrix_world.translation))
        scene.frame_set(old)
        for obj in _pv_stage_obstacles(scene):
            lo, hi = _pv_stage_bounds(obj)
            offset = previs_stage_obstacle_offset(lo, hi, path, center)
            if offset == (0, 0, 0):
                continue
            animated = obj.animation_data and (
                obj.animation_data.action or obj.animation_data.drivers
            )
            if (
                obj.parent is not None
                or animated
                or obj.constraints
                or obj.rigid_body
                or hi[2] <= center[2] + 0.3
            ):
                raise RuntimeError(
                    "Stage clearance failed: cannot automatically move floor, parented, constrained or animated object "
                    + obj.name
                )
            plans.append((obj, offset))
        # Plan everything before moving any object. Reject unsupported geometry
        # without partially changing the stage; preserve the authored flight path.
        moved = []
        for obj, offset in plans:
            obj.location += Vector(offset)
            moved.append({"name": obj.name, "offset": offset})
        bpy.context.view_layer.update()
        return {"checked": True, "moved": moved, "margin": 0.35, "camera_modified": False}
    finally:
        scene.frame_set(old)


def previs_validate_stage_camera(requirements):
    if not requirements.get("camera_choreography"):
        return {"checked": False}
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        raise RuntimeError("Complex camera validation failed: no scene camera")
    old = scene.frame_current
    positions = []
    aims = []
    roots = [o for o in scene.objects if o.get("previs_motion_version")]
    # Include all visible stage mesh structures. AABB tests are conservative;
    # split large/concave scenic meshes into separate blocks around the lane.
    obstacles = _pv_stage_obstacles(scene)
    try:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            p = tuple(camera.matrix_world.translation)
            previous = positions[-1] if positions else p
            if p[2] < 0.3:
                raise RuntimeError(
                    "Complex camera validation failed: camera below safe stage height"
                )
            for root in roots:
                q = root.matrix_world.translation
                if _pv_segment_box(
                    previous, p, (q.x - 0.9, q.y - 0.9, q.z), (q.x + 0.9, q.y + 0.9, q.z + 2.25)
                ):
                    raise RuntimeError(
                        "Complex camera validation failed: camera enters member safety volume at frame "
                        + str(frame)
                    )
            for obj in obstacles:
                lo, hi = _pv_stage_bounds(obj)
                if _pv_segment_box(previous, p, lo, hi):
                    raise RuntimeError(
                        "Complex camera validation failed: flight path intersects stage object "
                        + obj.name
                        + " at frame "
                        + str(frame)
                    )
            positions.append(p)
            aims.append(tuple(camera.matrix_world.to_quaternion() @ Vector((0, 0, -1))))
        spans = [max(p[i] for p in positions) - min(p[i] for p in positions) for i in range(3)]
        if min(spans[:2]) < 6 or spans[2] < 5:
            raise PrevisQualityError(
                "Complex camera validation failed: real dolly/orbit/crane motion required, not zoom-only"
            )
        if _motion_math.dist(aims[0], aims[-1]) > 0.3:
            raise PrevisQualityError(
                "Complex camera validation failed: final hero aim must return to front"
            )
        distances = [_motion_math.dist(a, b) for a, b in zip(positions, positions[1:])]
        fps = scene.render.fps / scene.render.fps_base
        duration = (scene.frame_end - scene.frame_start + 1) / fps
        if max(distances, default=0) * fps > 65 * 10 / duration:
            raise RuntimeError(
                "Complex camera validation failed: position jump relative to the shot duration"
            )
        return {
            "checked": True,
            "samples": len(positions),
            "translation_span": spans,
            "member_count": len(roots),
            "obstacles": len(obstacles),
        }
    finally:
        scene.frame_set(old)


"""Render-independent line-of-sight checks against scene geometry."""


def _previs_validate_visibility(requirements=None):
    """Inspect every frame and record sustained body occlusion as camera quality feedback."""
    import bpy
    import json
    import math
    from mathutils import Vector

    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        return {"checked": False, "reason": "No active camera", "subjects": []}
    names = json.loads(camera.get("previs_framing_subjects", "[]"))
    roots = [scene.objects.get(name) for name in names]
    roots = [root for root in roots if root is not None]
    if not roots:
        roots = [obj for obj in scene.objects if obj.get("previs_motion_version")]
    if not roots:
        return {"checked": False, "reason": "No registered subjects", "subjects": []}

    def is_subject(obj):
        while obj is not None:
            if obj in roots:
                return True
            obj = obj.parent
        return False

    def descendants(root):
        result = []
        for obj in scene.objects:
            current = obj
            while current is not None:
                if current == root:
                    if obj.type == "MESH" and not obj.hide_render:
                        result.append(obj)
                    break
                current = current.parent
        return result

    subjects = []
    for root in roots:
        meshes = descendants(root)
        head = next((obj for obj in meshes if "head" in obj.name.lower()), None)
        torso = scene.objects.get(root.get("previs_body_anchor", ""))
        if torso is None:
            torso = next((obj for obj in meshes if any(word in obj.name.lower()
                         for word in ("torso", "chest", "pelvis"))), None)
        markers = json.loads(root.get("previs_motion_markers", "{}"))
        hands = {name: scene.objects.get(markers.get(name, "")) for name in ("L_Hand", "R_Hand")}
        for name in hands:
            if hands[name] is None:
                side = name[0].lower()
                labels = (side + "_hand", "hand_" + side, ("left" if side == "l" else "right") + "_hand")
                hands[name] = next((obj for obj in meshes if any(label in obj.name.lower() for label in labels)), None)
        subjects.append({"root": root, "meshes": meshes, "head": head, "torso": torso,
                         "hands": {name: obj for name, obj in hands.items() if obj is not None}})
    dependency_graph = bpy.context.evaluated_depsgraph_get()

    def occluder(origin, target):
        delta = target - origin
        remaining = delta.length
        if remaining < 0.002:
            return None
        direction = delta / remaining
        position = origin.copy()
        for _ in range(64):
            if remaining < 0.002:
                return None
            hit, location, normal, face, obj, matrix = scene.ray_cast(
                dependency_graph, position, direction, distance=remaining - 0.001,
            )
            if not hit:
                return None
            original = getattr(obj, "original", obj)
            if not original.hide_render and not is_subject(original) and original != camera:
                return original.name
            advance = (location - position).length + 0.002
            position = position + direction * advance
            remaining -= advance
        return "Ray traversal limit"

    def center(obj):
        return obj.evaluated_get(dependency_graph).matrix_world.translation.copy()

    def body_points(subject):
        if subject["head"] is not None and subject["torso"] is not None:
            return [center(subject["head"]), center(subject["torso"])]
        corners = []
        for obj in subject["meshes"]:
            evaluated = obj.evaluated_get(dependency_graph)
            corners.extend(evaluated.matrix_world @ Vector(point) for point in evaluated.bound_box)
        if not corners:
            return []
        low = [min(point[axis] for point in corners) for axis in range(3)]
        high = [max(point[axis] for point in corners) for axis in range(3)]
        x, y = (low[0] + high[0]) / 2, (low[1] + high[1]) / 2
        return [Vector((x, y, low[2] + (high[2] - low[2]) * fraction)) for fraction in (.85, .55)]

    fps = scene.render.fps / getattr(scene.render, "fps_base", 1)
    threshold = max(2, math.ceil(fps * .5))
    results = [{"subject": subject["root"].name, "testedFrames": 0, "blockedBodyFrames": 0,
                "longestBlockedFrames": 0, "firstBlockedFrame": None, "occluders": set(),
                "handBlockedFrames": {name: 0 for name in subject["hands"]}}
               for subject in subjects]
    runs = [0] * len(subjects)
    previous_frame = scene.frame_current
    try:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            origin = center(camera)
            for index, subject in enumerate(subjects):
                record = results[index]
                points = body_points(subject)
                if not points:
                    continue
                record["testedFrames"] += 1
                blockers = [occluder(origin, point) for point in points]
                record["occluders"].update(name for name in blockers if name is not None)
                if all(name is not None for name in blockers):
                    record["blockedBodyFrames"] += 1
                    runs[index] += 1
                    record["longestBlockedFrames"] = max(record["longestBlockedFrames"], runs[index])
                    if record["firstBlockedFrame"] is None:
                        record["firstBlockedFrame"] = frame
                else:
                    runs[index] = 0
                for name, obj in subject["hands"].items():
                    blocker = occluder(origin, center(obj))
                    if blocker is not None:
                        record["handBlockedFrames"][name] += 1
                        record["occluders"].add(blocker)
    finally:
        scene.frame_set(previous_frame)
    failures = []
    for record in results:
        record["occluders"] = sorted(record["occluders"])
        record["longestBlockedSeconds"] = round(record["longestBlockedFrames"] / fps, 3)
        first = record["firstBlockedFrame"]
        record["firstBlockedTime"] = round((first - scene.frame_start) / fps, 3) if first is not None else None
        if record["longestBlockedFrames"] >= threshold:
            failures.append({key: record[key] for key in
                             ("subject", "firstBlockedFrame", "firstBlockedTime", "longestBlockedSeconds", "occluders")})
    report = {"checked": True, "passed": not failures, "frames": scene.frame_end - scene.frame_start + 1,
              "sustainedOcclusionSeconds": .5, "subjects": results}
    previs_quality_check(
        not failures,
        "Camera line of sight is blocked by scene geometry. Repair the camera path and retest visibility: "
        + json.dumps(failures, ensure_ascii=False),
        check="camera", key="camera-line-of-sight",
    )
    return report


"""Compatibility export for resource lists predating the consolidated camera checks."""


def previs_validate_visibility(requirements=None):
    return _previs_validate_visibility(requirements)
