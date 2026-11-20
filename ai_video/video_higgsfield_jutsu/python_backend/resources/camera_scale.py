"""Scene-relative camera paths, evaluated motion bounds and perspective framing."""


def previs_camera_scale_keys(
    keys,
    minimum,
    maximum,
    reference_span,
    reference_center=(0, 0, 0),
    scale_axes=(True, True, True),
):
    import math

    if not math.isfinite(reference_span) or reference_span <= 0:
        raise ValueError("reference_span must be positive and finite")
    if len(minimum) != 3 or len(maximum) != 3 or len(reference_center) != 3 or len(scale_axes) != 3:
        raise ValueError("camera scale requires three axes")
    if any(not math.isfinite(v) for v in (*minimum, *maximum, *reference_center)):
        raise ValueError("camera bounds must be finite")
    if any(b < a for a, b in zip(minimum, maximum)):
        raise ValueError("camera bounds are inverted")
    span = max(b - a for a, b in zip(minimum, maximum))
    if span <= 1e-6:
        raise ValueError("subject motion envelope is empty")
    factor = span / reference_span
    center = [(a + b) / 2 for a, b in zip(minimum, maximum)]
    return [
        (
            t,
            tuple(
                center[i] + (p[i] - reference_center[i]) * factor if scale_axes[i] else p[i]
                for i in range(3)
            ),
        )
        for t, p in keys
    ]


def previs_camera_fit_factor(frames, tan_x, tan_y, width=0.8, height=0.94):
    """Smallest radial distance multiplier fitting all camera-local point samples."""
    import math

    if not 0 < width < 1 or not 0 < height < 1 or min(tan_x, tan_y) <= 0:
        raise ValueError("invalid camera framing limits")
    required = 0.001
    for distance, points in frames:
        if not math.isfinite(distance) or distance <= 1e-6:
            raise ValueError("camera position equals target")
        for x, y, offset in points:
            required = max(
                required,
                (abs(x) / (tan_x * width) - offset) / distance,
                (abs(y) / (tan_y * height) - offset) / distance,
                (0.01 - offset) / distance,
            )
    return required


def previs_camera_from_scale(
    camera,
    subjects,
    positions,
    targets,
    reference_span,
    reference_center=(0, 0, 0),
    scale_axes=(True, True, True),
    fit_frame=True,
    frame_width=0.8,
    frame_height=0.94,
    start_frame=None,
    end_frame=None,
    focal_length=None,
):
    """Scale only camera curves using the union of all actual animated subject bounds.

    Curve times remain normalized. Fixed axes retain their exact world coordinates.
    Framing adjusts distance, never lens or subject animation. No collision guarantee.
    """
    import bpy
    import json
    import math
    from mathutils import Vector

    scene = bpy.context.scene
    start_frame = scene.frame_start if start_frame is None else start_frame
    end_frame = scene.frame_end if end_frame is None else end_frame
    if end_frame <= start_frame or not subjects:
        raise ValueError("camera scaling requires animated frames and actual subjects")
    if focal_length is not None:
        if focal_length <= 0:
            raise ValueError("invalid lens")
        camera.data.lens = focal_length
    objects = {
        obj.name: obj
        for root in subjects
        for obj in [root] + list(root.children_recursive)
        if obj.type in {"MESH", "CURVE", "SURFACE", "FONT", "META"} and not obj.hide_render
    }
    if not objects:
        raise ValueError("no visible subject geometry for camera scaling")
    old_frame = scene.frame_current
    frames = list(range(start_frame, end_frame + 1))
    samples = []
    try:
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            points = []
            for obj in objects.values():
                evaluated = obj.evaluated_get(depsgraph)
                points.extend(evaluated.matrix_world @ Vector(p) for p in evaluated.bound_box)
            samples.append(points)
        minimum = tuple(min(p[i] for points in samples for p in points) for i in range(3))
        maximum = tuple(max(p[i] for points in samples for p in points) for i in range(3))
        scaled_positions = previs_camera_scale_keys(
            positions, minimum, maximum, reference_span, reference_center, scale_axes
        )
        scaled_targets = previs_camera_scale_keys(
            targets, minimum, maximum, reference_span, reference_center, scale_axes
        )
        times = [(frame - start_frame) / (end_frame - start_frame) for frame in frames]
        path = [Vector(previs_camera_curve_sample(scaled_positions, t)) for t in times]
        aim = [Vector(previs_camera_curve_sample(scaled_targets, t)) for t in times]
        if fit_frame and any(scale_axes):
            if camera.data.type != "PERSP" or camera.data.shift_x or camera.data.shift_y:
                raise ValueError(
                    "automatic fit requires an unshifted perspective camera; disable fit for explicit optics"
                )
            corners = camera.data.view_frame(scene=scene)
            tan_x = max(abs(p.x / p.z) for p in corners)
            tan_y = max(abs(p.y / p.z) for p in corners)
            for attempt in range(12):
                local_frames = []
                for position, target, points in zip(path, aim, samples):
                    direction = target - position
                    distance = direction.length
                    if distance <= 1e-6:
                        raise ValueError("camera position equals target")
                    inverse = direction.to_track_quat("-Z", "Y").inverted()
                    local_points = [inverse @ (p - target) for p in points]
                    local_frames.append((distance, [(p.x, p.y, -p.z) for p in local_points]))
                adjustment = previs_camera_fit_factor(
                    local_frames, tan_x, tan_y, frame_width, frame_height
                )
                if abs(adjustment - 1) < 0.002:
                    break
                # Smaller iterations also support height-locked paths whose view angle changes.
                adjustment = max(0.5, min(2, adjustment))
                path = [
                    Vector(
                        tuple(
                            (
                                target[i] + (position[i] - target[i]) * adjustment
                                if scale_axes[i]
                                else position[i]
                            )
                            for i in range(3)
                        )
                    )
                    for position, target in zip(path, aim)
                ]
            else:
                raise RuntimeError(
                    "Camera scale fit could not converge within locked axes; revise path or framing constraints"
                )
        report = previs_camera_smooth(
            camera,
            list(zip(times, path)),
            list(zip(times, aim)),
            subjects,
            start_frame=start_frame,
            end_frame=end_frame,
            focal_length=focal_length,
        )
        fps = scene.render.fps / scene.render.fps_base
        duration = len(frames) / fps
        travel = sum((b - a).length for a, b in zip(path, path[1:]))
        report.update(
            subjectSpan=max(b - a for a, b in zip(minimum, maximum)),
            referenceSpan=reference_span,
            scale=max(b - a for a, b in zip(minimum, maximum)) / reference_span,
            travel=travel,
            duration=duration,
            averageSpeed=travel / duration,
            lateralSpan=max(p.x for p in path) - min(p.x for p in path),
        )
        camera["previs_scale_report"] = json.dumps(report)
        print(json.dumps(dict(event="previs_camera_scale", **report)))
        return camera
    finally:
        scene.frame_set(old_frame)
