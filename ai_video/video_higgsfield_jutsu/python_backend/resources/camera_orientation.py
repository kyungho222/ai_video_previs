"""Keep ordinary cameras upright while preserving world position and viewing direction."""


def previs_camera_upright(camera=None):
    import bpy
    from mathutils import Matrix, Vector

    scene = bpy.context.scene
    camera = camera or scene.camera
    if camera is None:
        raise RuntimeError("No active camera")
    if camera.get("previs_allow_roll", False):
        return {"corrected": False, "reason": "explicit_camera_roll"}
    old_frame = scene.frame_current
    samples = []
    previous_up, previous_q = None, None
    changed = 0
    try:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            graph = bpy.context.evaluated_depsgraph_get()
            matrix = camera.evaluated_get(graph).matrix_world.copy()
            position, rotation, scale = matrix.decompose()
            forward = (rotation @ Vector((0, 0, -1))).normalized()
            original_up = rotation @ Vector((0, 1, 0))
            up = Vector((0, 0, 1)) - forward * forward.z
            if up.length < 1e-4:
                up = previous_up.copy() if previous_up is not None else original_up.copy()
                up -= forward * up.dot(forward)
            if up.length < 1e-7:
                up = Vector((0, 1, 0)) - forward * forward.y
            up.normalize()
            right = forward.cross(up).normalized()
            up = right.cross(forward).normalized()
            upright = Matrix((right, up, -forward)).transposed().to_quaternion()
            if previous_q is not None and previous_q.dot(upright) < 0:
                upright.negate()
            changed += int(original_up.dot(up) < 0.9999)
            samples.append((frame, position, upright, scale))
            previous_up, previous_q = up.copy(), upright.copy()
        if not changed:
            return {"corrected": False, "frames": len(samples)}
        # A separate baked camera also handles parents/constraints without
        # changing their evaluated path or overwriting the source rig.
        corrected = camera.copy()
        # Object.copy also copies provider entity IDs. Only transfer camera
        # framing settings; the new object receives its own identity.
        for key in list(corrected.keys()):
            del corrected[key]
        for key in camera.keys():
            if key.startswith("previs_") and not key.endswith(("_id", "_uuid")):
                corrected[key] = camera[key]
        corrected.name = camera.name + "_Upright"
        corrected.animation_data_clear()
        corrected.constraints.clear()
        corrected.parent = None
        corrected.matrix_parent_inverse = Matrix.Identity(4)
        corrected.delta_location = (0, 0, 0)
        corrected.delta_rotation_euler = (0, 0, 0)
        corrected.delta_rotation_quaternion = (1, 0, 0, 0)
        corrected.delta_scale = (1, 1, 1)
        corrected.rotation_mode = "QUATERNION"
        scene.collection.objects.link(corrected)
        try:
            for frame, position, rotation, scale in samples:
                corrected.location = position
                corrected.rotation_quaternion = rotation
                corrected.scale = scale
                for path in ("location", "rotation_quaternion", "scale"):
                    corrected.keyframe_insert(data_path=path, frame=frame)
            for curve in _jutsu_action_curves(corrected.animation_data.action):
                for point in curve.keyframe_points:
                    point.interpolation = "LINEAR"
            scene.camera = corrected
        except BaseException:
            bpy.data.objects.remove(corrected, do_unlink=True)
            raise
        return {"corrected": True, "correctedFrames": changed, "frames": len(samples),
                "camera": corrected.name}
    finally:
        scene.frame_set(old_frame)
