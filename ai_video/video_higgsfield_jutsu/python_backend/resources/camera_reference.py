"""Render a camera/staging reference while removing articulated character poses."""


def render_camera_staging_reference():
    import bpy
    import hashlib
    import json
    import re
    from mathutils import Vector

    scene = bpy.context.scene
    if scene.camera is None:
        raise RuntimeError("Camera reference requires an existing scene camera")
    original_objects = list(scene.objects)
    roots = [obj for obj in original_objects
             if obj.get("previs_character") or obj.get("previs_motion_version")]
    if not roots:
        raise RuntimeError("Camera reference requires registered character roots")
    old_frame = scene.frame_current
    old_active = bpy.context.view_layer.objects.active
    selected = [obj for obj in original_objects if obj.select_get()]
    created, meshes, materials = [], [], []
    hidden = {obj: obj.hide_render for obj in original_objects}
    settings = []

    def change(owner, name, value):
        settings.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def geometry_bounds(objects, graph):
        points = [obj.evaluated_get(graph).matrix_world @ Vector(corner)
                  for obj in objects if obj.type == "MESH"
                  for corner in obj.evaluated_get(graph).bound_box]
        if not points:
            raise RuntimeError("Character reference has no mesh bounds")
        return (Vector(tuple(min(point[i] for point in points) for i in range(3))),
                Vector(tuple(max(point[i] for point in points) for i in range(3))))

    def sphere(name, scale, material):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=1)
        obj = bpy.context.object
        created.append(obj)
        meshes.append(obj.data)
        obj.name = name
        obj.scale = scale
        obj.data.materials.append(material)
        return obj

    try:
        scene.frame_set(scene.frame_start)
        graph = bpy.context.evaluated_depsgraph_get()
        characters = []
        character_members = set()
        for index, root in enumerate(roots):
            members = [root, *root.children_recursive]
            character_members.update(members)
            minimum, maximum = geometry_bounds(members, graph)
            height = max(0.2, maximum.z - minimum.z)
            tagged = bpy.data.objects.get(root.get("previs_body_anchor", ""))
            anchors = [obj for obj in members if obj.type == "MESH" and
                       re.search(r"pelvis|hips|shorts|torso|chest|body", obj.name, re.I)]
            anchor = tagged if tagged in members else next(
                (obj for obj in anchors if re.search(r"pelvis|hips|shorts", obj.name, re.I)),
                anchors[0] if anchors else None,
            )
            initial = (anchor.evaluated_get(graph).matrix_world.translation.copy()
                       if anchor else (minimum + maximum) / 2)
            material = bpy.data.materials.new("CameraStaging_Material")
            materials.append(material)
            source_material = anchor.active_material if anchor else None
            material.diffuse_color = (source_material.diffuse_color
                                      if source_material else (0.45, 0.55, 0.65, 1))
            body = sphere("CameraStaging_Body", (0.14 * height, 0.11 * height, 0.44 * height), material)
            head = sphere("CameraStaging_Head", (0.10 * height,) * 3, material)
            characters.append({"root": root, "anchor": anchor, "members": members,
                               "initial": initial, "floor": minimum.z, "height": height,
                               "body": body, "head": head})
        for obj in original_objects:
            dynamic = False
            parent = obj
            while parent is not None and parent is not scene.camera:
                if parent.animation_data and parent.animation_data.action:
                    dynamic = True
                    break
                parent = parent.parent
            if obj in character_members or (obj.type in {"MESH", "CURVE", "FONT", "META", "SURFACE"} and dynamic):
                obj.hide_render = True
        camera_hash = hashlib.sha256()
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            graph = bpy.context.evaluated_depsgraph_get()
            camera_hash.update(json.dumps(
                [[round(float(value), 7) for value in row]
                 for row in scene.camera.evaluated_get(graph).matrix_world],
                separators=(",", ":"),
            ).encode())
            for item in characters:
                if item["anchor"]:
                    position = item["anchor"].evaluated_get(graph).matrix_world.translation
                else:
                    minimum, maximum = geometry_bounds(item["members"], graph)
                    position = (minimum + maximum) / 2
                delta = position - item["initial"]
                for obj, fraction in ((item["body"], 0.44), (item["head"], 0.90)):
                    obj.location = (item["initial"].x + delta.x, item["initial"].y + delta.y,
                                    item["floor"] + delta.z + fraction * item["height"])
                    obj.keyframe_insert(data_path="location", frame=frame)
        for obj in created:
            for curve in _jutsu_action_curves(obj.animation_data.action):
                for key in curve.keyframe_points:
                    key.interpolation = "LINEAR"
        change(scene.render, "engine", "BLENDER_WORKBENCH")
        change(scene.render, "resolution_percentage", 100)
        width, height = _jutsu_render_size(scene.render.resolution_x, scene.render.resolution_y)
        change(scene.render, "resolution_x", width)
        change(scene.render, "resolution_y", height)
        change(scene.display.shading, "color_type", "MATERIAL")
        change(scene.display.shading, "show_shadows", False)
        change(scene.display.shading, "show_cavity", False)
        settings.append((scene.render.image_settings, "file_format", scene.render.image_settings.file_format))
        change(scene.render.image_settings, "media_type", "VIDEO")
        scene.render.image_settings.file_format = "FFMPEG"
        change(scene.render.ffmpeg, "format", "MPEG4")
        change(scene.render.ffmpeg, "codec", "H264")
        target = _jutsu_artifact_file(name="camera-staging.mp4", media_type="video/mp4")
        change(scene.render, "filepath", str(target.path))
        bpy.ops.render.render(animation=True)
        target.publish()
        return {"name": "camera-staging.mp4", "mediaType": "video/mp4",
                "referenceKind": "camera_staging", "characters": len(characters),
                "cameraMatrixSha256": camera_hash.hexdigest(),
                "trajectorySources": ["body_anchor" if item["anchor"] else "bounds_estimate"
                                      for item in characters],
                "fps": scene.render.fps / scene.render.fps_base,
                "frames": scene.frame_end - scene.frame_start + 1}
    finally:
        for owner, name, value in reversed(settings):
            setattr(owner, name, value)
        for obj, value in hidden.items():
            obj.hide_render = value
        for obj in created:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in meshes:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        for material in materials:
            if material.users == 0:
                bpy.data.materials.remove(material)
        scene.frame_set(old_frame)
        for obj in original_objects:
            obj.select_set(obj in selected)
        bpy.context.view_layer.objects.active = old_active
