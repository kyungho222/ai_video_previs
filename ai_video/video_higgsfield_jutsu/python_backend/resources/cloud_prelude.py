import bpy as _jutsu_bpy
import shutil as _jutsu_shutil
import time as _jutsu_time
import math as _jutsu_math
import json as _jutsu_json
import uuid as _jutsu_uuid

_jutsu_artifact_cache = {}
_jutsu_rendered = False
_jutsu_render_result = None
_jutsu_motion_requirements = {}


class PrevisQualityError(RuntimeError):
    """Non-structural quality findings are deferred until after scene commit."""


def previs_quality_report():
    scene = _jutsu_bpy.context.scene
    raw = getattr(scene, "get", lambda key, default: default)("_jutsu_quality_issues", "[]")
    issues = _jutsu_json.loads(raw)
    return {"checked": True, "passed": not issues, "issues": issues}


def previs_reset_quality_issues(check=None):
    scene = _jutsu_bpy.context.scene
    issues = (
        []
        if check is None
        else [item for item in previs_quality_report()["issues"] if item.get("check") != check]
    )
    scene["_jutsu_quality_issues"] = _jutsu_json.dumps(issues, ensure_ascii=False)


def previs_quality_check(condition, message, *, check="scene", key=None):
    scene = _jutsu_bpy.context.scene
    message = str(message)[:2000]
    key = str(key if key is not None else message)
    issues = [
        item
        for item in previs_quality_report()["issues"]
        if (item.get("check"), item.get("key")) != (check, key)
    ]
    if not condition:
        issues.append({"check": check, "key": key, "message": message})
        print("JUTSU_BUILD quality_warning " + check + ": " + message, flush=True)
    scene["_jutsu_quality_issues"] = _jutsu_json.dumps(issues[-128:], ensure_ascii=False)
    return bool(condition)


def previs_validate_build_quality(requirements):
    report = previs_quality_report()
    if not report["passed"]:
        raise PrevisQualityError(
            "Deferred scene quality checks: "
            + _jutsu_json.dumps(report["issues"], ensure_ascii=False)
        )
    return report


def previs_spatial_manifest():
    scene = _jutsu_bpy.context.scene
    objects = list(_jutsu_bpy.data.objects)
    for obj in objects:
        if not obj.get("previs_entity_id"):
            obj["previs_entity_id"] = _jutsu_uuid.uuid4().hex
    camera = scene.camera
    names = set(_jutsu_json.loads(camera.get("previs_framing_subjects", "[]"))) if camera else set()
    subjects = [obj for obj in objects if obj.name in names]
    if not subjects:
        subjects = [obj for obj in globals().get("subjects", []) if obj in objects]
    fps = scene.render.fps / getattr(scene.render, "fps_base", 1)
    manifest = {
        "version": 1,
        "source": "blender",
        "fps": fps,
        "duration": (scene.frame_end - scene.frame_start + 1) / fps,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "objects": [],
        "frames": [],
    }
    for obj in objects:
        manifest["objects"].append(
            {
                "id": obj["previs_entity_id"],
                "name": obj.name,
                "type": obj.type,
                "parentId": obj.parent.get("previs_entity_id") if obj.parent else None,
                "role": (
                    "subject" if obj in subjects else "camera" if obj is camera else "scene_object"
                ),
            }
        )

    def transform(obj):
        return {
            "id": obj["previs_entity_id"],
            "location": [round(float(value), 6) for value in obj.matrix_world.translation],
            "matrixWorld": [[round(float(value), 6) for value in row] for row in obj.matrix_world],
        }

    old = scene.frame_current
    try:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            _jutsu_bpy.context.view_layer.update()
            value = {
                "frame": frame,
                "time": round((frame - scene.frame_start) / fps, 6),
                "subjects": [transform(obj) for obj in subjects],
            }
            if camera:
                value["camera"] = {**transform(camera), "lens": float(camera.data.lens)}
            manifest["frames"].append(value)
    finally:
        scene.frame_set(old)
    return manifest


def _jutsu_export_spatial_manifest():
    manifest = previs_spatial_manifest()
    target = _jutsu_artifact_file(name="previs-spatial.json", media_type="application/json")
    with open(str(target.path), "w", encoding="utf-8") as file:
        _jutsu_json.dump(manifest, file, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    target.publish()
    return {
        "name": "previs-spatial.json",
        "mediaType": "application/json",
        "frames": len(manifest["frames"]),
    }


def _jutsu_set_look(settings, name):
    candidates = [name]
    for prefix in ("AgX - ", "Filmic - "):
        if name.startswith(prefix):
            candidates.append(name[len(prefix) :])
    last_error = None
    for candidate in candidates:
        try:
            settings.look = candidate
            return candidate
        except (TypeError, ValueError) as error:
            last_error = error
    raise last_error


class _JutsuArtifact:
    def __init__(self, target, media_type):
        self._target = target
        self.media_type = media_type
        self._published = False
        self._publication = None

    def __getattr__(self, name):
        return getattr(self._target, name)

    def publish(self):
        if not self._published:
            self._publication = self._target.publish()
            self._published = True
        return self._publication


def _jutsu_artifact_file(*, name, media_type):
    if name in _jutsu_artifact_cache:
        target = _jutsu_artifact_cache[name]
        if target.media_type != media_type:
            raise RuntimeError("Artifact media type mismatch: " + name)
        return target
    target = _JutsuArtifact(artifacts.file(name=name, media_type=media_type), media_type)
    _jutsu_artifact_cache[name] = target
    return target


def _jutsu_video_format(settings):
    settings.media_type = "VIDEO"
    settings.file_format = "FFMPEG"


def _jutsu_world(scene):
    if scene.world is None:
        scene.world = _jutsu_bpy.data.worlds.new("Previs_World")
    return scene.world


def _jutsu_action_curves(action):
    if action is None:
        return []
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return legacy
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                curves.extend(bag.fcurves)
    return curves


def _jutsu_copy_to_artifact(target, source):
    _jutsu_shutil.copyfile(source, target.path)


def _jutsu_render_size(width, height, max_side=640, max_pixels=230400):
    if width < 2 or height < 2:
        raise ValueError("Previs render dimensions must be at least two pixels")
    scale = min(1, max_side / max(width, height), _jutsu_math.sqrt(max_pixels / (width * height)))
    x = max(2, 2 * round(width * scale / 2))
    y = max(2, 2 * round(height * scale / 2))
    while x * y > max_pixels or max(x, y) > max_side:
        if x >= y:
            x -= 2
        else:
            y -= 2
    return x, y


def render_previs(*, allow_quality_warnings=True):
    global _jutsu_rendered, _jutsu_render_result
    if _jutsu_rendered:
        return _jutsu_render_result
    scene = _jutsu_bpy.context.scene
    if scene.camera is None:
        raise RuntimeError("Cannot render the current scene: no active camera exists.")
    started = _jutsu_time.monotonic()
    upright_report = previs_camera_upright(scene.camera)
    if upright_report.get("corrected"):
        print("JUTSU_RENDER camera_upright " + _jutsu_json.dumps(upright_report), flush=True)
    previs_restore_character_limbs()
    print("JUTSU_RENDER validation_start", flush=True)
    validation_warnings = []

    def quality_check(name, validator):
        try:
            return validator(_jutsu_motion_requirements)
        except PrevisQualityError as error:
            if not allow_quality_warnings:
                raise
            warning = {"category": "quality", "action": "continue_with_warning", "check": name, "message": str(error) or type(error).__name__}
            validation_warnings.append(warning)
            print("JUTSU_RENDER quality_warning " + name + ": " + warning["message"], flush=True)
            return {"checked": True, "passed": False, "warning": warning["message"]}

    visibility_report = previs_validate_visibility(_jutsu_motion_requirements)
    build_report = quality_check("scene_build", previs_validate_build_quality)
    motion_report = quality_check("motion", previs_validate_scene_motion)
    camera_report = quality_check("stage_camera", previs_validate_stage_camera)
    general_camera_report = quality_check("camera", previs_validate_camera)
    vehicle_report = quality_check("vehicle", previs_validate_vehicle_action)
    dog_report = quality_check("dog_mirror", previs_validate_dog_mirror)
    validation_seconds = _jutsu_time.monotonic() - started
    requested = [
        getattr(scene.render, "resolution_x", 640),
        getattr(scene.render, "resolution_y", 360),
    ]
    scene.render.resolution_x, scene.render.resolution_y = _jutsu_render_size(*requested)
    settings = {
        "requestedResolution": requested,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "fps": getattr(scene.render, "fps", 24),
        "frameRange": [getattr(scene, "frame_start", 1), getattr(scene, "frame_end", 1)],
        "pixelBudget": 230400,
        "shadows": False,
        "cavity": False,
    }
    print(
        "JUTSU_RENDER validation_complete seconds=" + str(round(validation_seconds, 3)), flush=True
    )
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.show_shadows = False
    scene.display.shading.show_cavity = False
    scene.render.resolution_percentage = 100
    scene.render.image_settings.media_type = "VIDEO"
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    target = _jutsu_artifact_file(name="previs.mp4", media_type="video/mp4")
    scene.render.filepath = str(target.path)
    render_started = _jutsu_time.monotonic()
    print("JUTSU_RENDER render_start settings=" + str(settings), flush=True)
    _jutsu_bpy.ops.render.render(animation=True)
    render_seconds = _jutsu_time.monotonic() - render_started
    print("JUTSU_RENDER render_complete seconds=" + str(round(render_seconds, 3)), flush=True)
    target.publish()
    print("JUTSU_RENDER published", flush=True)
    spatial_manifest = None
    try:
        spatial_manifest = _jutsu_export_spatial_manifest()
    except Exception as error:
        validation_warnings.append(
            {"check": "spatial_manifest", "message": str(error) or type(error).__name__}
        )
    _jutsu_rendered = True
    _jutsu_render_result = {
        "name": "previs.mp4",
        "mediaType": "video/mp4",
        "buildQualityValidation": build_report,
        "motionValidation": motion_report,
        "cameraUpright": upright_report,
        "cameraValidation": camera_report,
        "generalCameraValidation": general_camera_report,
        "visibilityValidation": visibility_report,
        "vehicleValidation": vehicle_report,
        "dogMirrorValidation": dog_report,
        "spatialManifest": spatial_manifest,
        "validationWarnings": validation_warnings,
        "renderSettings": settings,
        "validationSeconds": round(validation_seconds, 3),
        "renderSeconds": round(render_seconds, 3),
    }
    return _jutsu_render_result
