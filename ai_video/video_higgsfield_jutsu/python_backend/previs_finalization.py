"""Bounded scene quality review, render finalization and artifact recovery."""

import ast
import json
from uuid import uuid4

from .cloud_python import prepare_cloud_python, motion_requirements


from .remote_tools import (
    tool_kind,
    result_data,
    call_tool,
    settle_operation,
)


SCENE_REVIEW_LIMIT = 5
INITIAL_BUILD_LIMIT = 3


CAMERA_QUALITY_REPORT_CODE = """
_jutsu_visibility_report = previs_validate_visibility(_jutsu_motion_requirements)
print(_jutsu_json.dumps({"event": "previs_visibility", "report": _jutsu_visibility_report}))
_jutsu_camera_issues = [
    {"key": issue["key"], "message": issue["message"][:400]}
    for issue in previs_quality_report()["issues"]
    if issue.get("check") == "camera"
]
print(_jutsu_json.dumps({"event": "previs_camera_quality", "issues": _jutsu_camera_issues[:8]}))
"""


def camera_quality_feedback(job, project_id):
    report = job.get("cameraQuality") or {}
    if report.get("projectId") != project_id or not report.get("issues"):
        return None
    return {
        "code": "CAMERA_QUALITY_REPAIR_REQUIRED",
        "issues": report["issues"],
        "message": (
            "Use the committed scene and its current revision and scene sequence. "
            "Spend the next available review on a camera-only correction with a new operationId. "
            "Keep the existing subjects and their animation. Fix the reported framing or movement "
            "issues, including camera-to-subject line-of-sight obstructions. Preserve subjects and environment; "
            "choose a clear continuous camera route and recheck previs_validate_visibility() after the final camera changes "
            "before requesting the render. Respect intentional occlusion explicitly requested by the user."
        ),
    }


def capture_spatial_manifest(job, content, definitions):
    operation = content.get("operation") or {}
    if operation.get("status") != "succeeded":
        return
    tool = next((item for item in definitions if tool_kind(item["name"]) == "get_artifact"), None)
    if not tool:
        return
    for artifact in operation.get("generatedArtifacts", []):
        if (
            artifact.get("name") == "previs-spatial.json"
            and artifact.get("mediaType") == "application/json"
            and artifact.get("id")
        ):
            job["spatialManifestRef"] = {
                "tool": tool["name"],
                "arguments": {
                    "projectId": operation["projectId"],
                    "operationId": operation["id"],
                    "artifactId": artifact["id"],
                },
            }


def render_only_code(code):
    """Only the plain render helper call is exempt from the scene edit budget."""
    try:
        body = ast.parse(code).body
    except (SyntaxError, TypeError):
        return False
    if len(body) != 1:
        return False
    statement = body[0]
    if isinstance(statement, ast.Assign):
        if not all(isinstance(target, ast.Name) for target in statement.targets):
            return False
        value = statement.value
    elif isinstance(statement, ast.Expr):
        value = statement.value
    else:
        return False
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "render_previs"
        and not value.args
        and all(isinstance(keyword.value, ast.Constant) for keyword in value.keywords)
    )


def count_scene_review(job, kind, code, emit):
    budget = job.setdefault(
        "reviewBudget",
        {
            "limit": SCENE_REVIEW_LIMIT,
            "used": 0,
            "buildAttempted": False,
            "buildCommitted": False,
            "initialBuildAttempts": 0,
        },
    )
    mutation = kind == "run_python" and not render_only_code(code)
    if not budget.get("buildCommitted"):
        if mutation:
            budget["buildAttempted"] = True
            budget["initialBuildAttempts"] = budget.get("initialBuildAttempts", 0) + 1
            emit("checkpoint", None)
            emit(
                "log",
                f"[previs-build] 초기 장면 실행 {budget['initialBuildAttempts']}/{INITIAL_BUILD_LIMIT}",
            )
        return
    if mutation or kind in ("query_python", "get_project", "get_glb", "show_scene", "import_asset"):
        budget["used"] += 1
        emit("checkpoint", None)
        emit("log", f"[previs-review] 장면 조회·수정 {budget['used']}/{budget['limit']}")


async def finalize_current_previs(client, job, history, emit, *, settled_operation=None):
    """Retrieve a published video, or render the last settled scene, without another AI turn."""
    if job.get("unresolvedValidation"):
        emit("checkpoint", None)
        raise RuntimeError("실행·필수 코드 검증 문제가 해결되지 않아 렌더링을 중단합니다: "
                           + json.dumps(job["unresolvedValidation"], ensure_ascii=False))
    if any(media.get("mediaType") == "video/mp4" for media in job.get("media", [])):
        return {"message": "프리비즈 MP4 생성이 완료되었습니다.", "media": job["media"]}
    projects = job.get("remoteProjects", [])
    if not projects:
        raise RuntimeError("조회·수정을 마쳤지만 생성된 원격 장면이 없어 렌더링할 수 없습니다.")
    project_id = projects[-1]
    emit(
        "log",
        "[previs-finalize] 추가 조회·수정을 종료하고 현재 장면의 영상 생성·회수를 진행합니다.",
    )
    job["finalization"] = {"projectId": project_id, "state": "checking"}
    emit("checkpoint", None)

    async def invoke(kind, arguments):
        tool = next((tool for tool in client.definitions if tool_kind(tool["name"]) == kind), None)
        if tool is None:
            raise RuntimeError("결과 마무리에 필요한 도구가 없습니다: " + kind)
        emit(
            "payload",
            {"endpoint": client.status["endpoint"], "tool": tool["name"], "arguments": arguments},
        )
        response = await call_tool(client, tool["name"], arguments, emit)
        emit("response", response)
        response = await settle_operation(client, response, emit)
        content = result_data(response)
        if not isinstance(content, dict):
            raise RuntimeError("결과 마무리 도구 응답 형식이 올바르지 않습니다: " + kind)
        if content.get("ok") is False or (content.get("operation") or {}).get("status") in (
            "failed",
            "timed_out",
            "expired",
        ):
            error = content.get("error") or (content.get("operation") or {}).get("error") or content
            failure = RuntimeError(
                "현재 장면의 결과 마무리 실패: " + json.dumps(error, ensure_ascii=False)
            )
            failure.tool_response = content
            raise failure
        return content, tool["name"]

    async def retrieve(operation):
        capture_spatial_manifest(job, {"operation": operation}, client.definitions)
        videos = [
            artifact
            for artifact in operation.get("generatedArtifacts", [])
            if artifact.get("mediaType") == "video/mp4" and artifact.get("id")
        ]
        if not videos:
            return None
        arguments = {
            "projectId": project_id,
            "operationId": operation["id"],
            "artifactId": videos[-1]["id"],
        }
        content, name = await invoke("get_artifact", arguments)
        if (content.get("artifact") or {}).get("mediaType") != "video/mp4" or (
            content.get("download") or {}
        ).get("method") != "GET":
            raise RuntimeError("생성된 MP4 다운로드 정보를 확인할 수 없습니다.")
        item = {
            "name": content["artifact"].get("name") or "previs.mp4",
            "mediaType": "video/mp4",
            "download": content["download"],
            "artifactRef": {"tool": name, "arguments": arguments},
        }
        job.setdefault("media", []).append(item)
        warnings = (operation.get("result") or {}).get("validationWarnings", [])
        job["finalization"].update(
            state="completed", operationId=operation["id"], qualityWarnings=warnings
        )
        emit("checkpoint", None)
        message = "프리비즈 MP4 생성이 완료되었습니다."
        if warnings:
            emit(
                "log",
                f"[previs-finalize] 품질 검증 경고 {len(warnings)}개를 기록하고 영상을 반환합니다.",
            )
            message += f" 품질 검증 경고 {len(warnings)}개가 남아 있습니다."
        return {"message": message, "media": job["media"]}

    if (
        settled_operation
        and settled_operation.get("status") == "succeeded"
        and settled_operation.get("projectId") == project_id
        and type(settled_operation.get("revisionAfter")) is int
        and type(settled_operation.get("sceneSequenceAfter")) is int
    ):
        result = await retrieve(settled_operation)
        if result:
            return result
        project = {
            "revision": settled_operation["revisionAfter"],
            "sceneSequence": settled_operation["sceneSequenceAfter"],
        }
    else:
        content, _ = await invoke("get_project", {"projectId": project_id})
        project = content.get("project") or content
        active = project.get("activeOperationId")
        last = next(
            (
                operation
                for operation in reversed(job.get("codeOperations", []))
                if operation.get("projectId") == project_id
            ),
            {},
        )
        operation_id = active or last.get("operationId")
        if operation_id:
            # Resolve even a previously submitted/unknown mutation; never blindly replay it.
            tool = next(
                (tool for tool in client.definitions if tool_kind(tool["name"]) == "get_operation"),
                None,
            )
            if tool is None:
                raise RuntimeError("마지막 원격 작업을 확인할 get_operation 도구가 없습니다.")
            arguments = {"projectId": project_id, "operationId": operation_id}
            emit(
                "payload",
                {
                    "endpoint": client.status["endpoint"],
                    "tool": tool["name"],
                    "arguments": arguments,
                },
            )
            response = await call_tool(client, tool["name"], arguments, emit)
            emit("response", response)
            settled = result_data(await settle_operation(client, response, emit)) or {}
            operation = settled.get("operation") or {}
            if operation.get("status") not in ("succeeded", "failed", "timed_out", "expired"):
                raise RuntimeError(
                    "마지막 원격 작업의 완료 여부가 불명확하여 중복 렌더링하지 않았습니다."
                )
            if operation.get("status") == "succeeded" and (
                active or operation.get("revisionAfter") == project.get("revision")
            ):
                result = await retrieve(operation)
                if result:
                    return result
            elif operation.get("status") != "succeeded":
                raise RuntimeError(
                    "마지막 원격 작업 실패: "
                    + json.dumps(settled.get("error") or operation.get("error"), ensure_ascii=False)
                )
            if active:
                content, _ = await invoke("get_project", {"projectId": project_id})
                project = content.get("project") or content
    revision, sequence = project.get("revision"), project.get("sceneSequence")
    if type(revision) is not int or type(sequence) is not int or revision < 1:
        failures = [
            item for item in job.get("repairHistory", []) if item.get("projectId") == project_id
        ]
        detail = (
            json.dumps(failures[-1]["error"], ensure_ascii=False)
            if failures
            else "초기 장면 생성이 완료되지 않았습니다."
        )
        raise RuntimeError("초기 장면을 확정하지 못해 렌더링할 수 없습니다. 마지막 원인: " + detail)
    operation_id = "finalize-previs-" + uuid4().hex[:16]
    render_code = "result=render_previs(allow_quality_warnings=True)"
    arguments = {
        "projectId": project_id,
        "operationId": operation_id,
        "revision": revision,
        "expectedSceneSequence": sequence,
        "waitSeconds": 8,
        "code": prepare_cloud_python(
            render_code,
            requirements=motion_requirements(
                history,
                detailed_motion=(job.get("plan") or {}).get("motion_detail") == "requested_detail",
            ),
        ),
    }
    record = {
        "projectId": project_id,
        "operationId": operation_id,
        "status": "submitted",
        "code": render_code,
    }
    job.setdefault("codeOperations", []).append(record)
    job["finalization"].update(state="rendering", operationId=operation_id)
    emit("checkpoint", None)
    try:
        content, _ = await invoke("run_python", arguments)
    except Exception as error:
        response = getattr(error, "tool_response", {})
        if isinstance(response, dict) and "operation" not in response:
            response = result_data(response) or response
        status = (
            (response.get("operation") or {}).get("status") if isinstance(response, dict) else None
        )
        record["status"] = "failed" if status in ("failed", "timed_out", "expired") else "unknown"
        emit("checkpoint", None)
        raise
    record["status"] = "succeeded"
    result = await retrieve(content.get("operation") or {})
    if result:
        return result
    raise RuntimeError("렌더링은 종료됐지만 게시된 MP4 파일 정보를 찾지 못했습니다.")

