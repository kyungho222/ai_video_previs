"""Provider-independent camera/staging video preparation from a native 3D scene."""

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from .agent import call_tool, result_data, settle_operation, tool_kind
from .cloud_python import prepare_cloud_python
from .video_errors import VideoError, MAX_REFERENCE_BYTES
from .video_media import download_video, inspect_video


def source_scene_reference(service, descriptor):
    if descriptor.get("jobId"):
        row = service.store.get(descriptor["jobId"])
        if row:
            return row["data"]
        return service.archive.scene_reference(descriptor["jobId"])
    if descriptor.get("recoveredId"):
        try:
            path = service.archive.safe_path(
                Path("runtime/recovered") / (descriptor["recoveredId"] + ".json")
            )
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pass
    return {}


def scene_project_id(service, descriptor, explicit=None):
    data = source_scene_reference(service, descriptor)
    projects = data.get("remoteProjects") or []
    media_index = descriptor.get("mediaIndex", 0)
    mapped_project = (data.get("mediaProjects") or {}).get(str(media_index))
    media = data.get("media") or []
    if mapped_project is None and type(media_index) is int and 0 <= media_index < len(media):
        mapped_project = ((media[media_index].get("artifactRef") or {}).get("arguments") or {}).get("projectId")
    project_id = explicit or mapped_project or (projects[-1] if projects else None)
    if project_id is None:
        project_id = ((data.get("spatialManifestRef") or {}).get("arguments") or {}).get("projectId")
    try:
        return str(UUID(project_id))
    except (ValueError, TypeError, AttributeError):
        raise VideoError(
            "카메라·배치 전용 영상에는 원본 3D 장면이 필요합니다. 원본 프로젝트가 연결된 프리비즈를 사용하세요."
        ) from None


async def prepare_camera_reference(client, project_id, target, record, save, progress):
    """Persist a render operation before submission and resume it by operation ID."""
    await client.start()
    async with asyncio.timeout(180):
        while client.status["state"] == "connecting":
            await asyncio.sleep(0.2)
    if client.status["state"] != "connected":
        raise VideoError(client.status["message"])

    async def invoke(kind, arguments):
        tool = next((tool for tool in client.definitions if tool_kind(tool["name"]) == kind), None)
        if tool is None:
            raise VideoError("카메라 참조 생성에 필요한 3D 도구가 없습니다: " + kind)
        response = await call_tool(client, tool["name"], arguments)
        value = result_data(response)
        if not isinstance(value, dict) or value.get("ok") is False:
            raise VideoError("카메라 참조 작업의 상태 또는 자료를 확인할 수 없습니다.")
        return response, value

    def emit(kind, value):
        if kind == "log":
            progress(value)

    if record and record.get("projectId") != project_id:
        raise VideoError("재개할 카메라 참조 작업의 프로젝트가 일치하지 않습니다.")
    if record and record.get("operationId"):
        response, _ = await invoke("get_operation", {
            "projectId": project_id, "operationId": record["operationId"],
        })
    else:
        _, data = await invoke("get_project", {"projectId": project_id})
        project = data.get("project") or data
        revision, sequence = project.get("revision"), project.get("sceneSequence")
        if type(revision) is not int or type(sequence) is not int or revision < 1:
            raise VideoError("카메라 참조를 렌더링할 확정 3D 장면이 없습니다.")
        record = {"version": 1, "projectId": project_id,
                  "operationId": "camera-reference-" + uuid4().hex[:16], "state": "submitted"}
        save(dict(record))
        response, _ = await invoke("run_python", {
            "projectId": project_id, "operationId": record["operationId"],
            "revision": revision, "expectedSceneSequence": sequence, "waitSeconds": 8,
            "code": prepare_cloud_python("result=render_camera_staging_reference()"),
        })
    response = await settle_operation(client, response, emit)
    data = result_data(response) or {}
    operation = data.get("operation") or {}
    if operation.get("status") != "succeeded":
        raise VideoError("카메라·배치 전용 MP4 렌더링이 완료되지 않았습니다. 원격 작업 상태를 확인하세요.")
    artifacts = [item for item in operation.get("generatedArtifacts", [])
                 if item.get("mediaType") == "video/mp4"]
    artifact = next((item for item in artifacts if item.get("name") == "camera-staging.mp4"),
                    artifacts[0] if len(artifacts) == 1 else None)
    if not artifact or not artifact.get("id"):
        raise VideoError("카메라·배치 MP4 결과를 찾지 못했습니다.")
    _, output = await invoke("get_artifact", {
        "projectId": project_id, "operationId": record["operationId"], "artifactId": artifact["id"],
    })
    download = output.get("download") or {}
    if download.get("method") != "GET" or not download.get("url"):
        raise VideoError("카메라·배치 MP4 다운로드 정보를 확인할 수 없습니다.")
    progress("카메라·배치 전용 MP4를 저장합니다.")
    # Owner-specific target keeps an expired worker from replacing a new worker's input.
    await download_video(download["url"], target, headers=download.get("headers"),
                         max_bytes=MAX_REFERENCE_BYTES)
    info = inspect_video(target, max_duration=None, min_fps=0, check_dimensions=False)
    details = operation.get("result") or {}
    record = {**record, "state": "ready", "duration": info["duration"],
              "referenceKind": "camera_staging", "render": details}
    try:
        save(record)
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise
    return Path(target)
