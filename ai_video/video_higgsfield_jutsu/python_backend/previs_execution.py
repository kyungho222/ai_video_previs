"""Execute the planned remote scene with bounded correction and finalization."""

import ast
import json
import re
import time
from uuid import uuid4

import httpx
from .prompt_filter import apply_presentation_rules
from .code_validation import validate_generated_code
from .previs_workers import validate_camera_integration, repair_previs_functions
from .cloud_python import (
    instructions,
    prepare_cloud_python,
    split_scene_render,
    motion_requirements,
    COMBINED_SCENE_CAMERA_HELPERS,
)


from .remote_tools import (
    tool_kind,
    result_data,
    repairable_operation_failure,
    call_tool,
    settle_operation,
)
from .previs_finalization import (
    SCENE_REVIEW_LIMIT,
    INITIAL_BUILD_LIMIT,
    CAMERA_QUALITY_REPORT_CODE,
    camera_quality_feedback,
    capture_spatial_manifest,
    render_only_code,
    count_scene_review,
    finalize_current_previs,
)
from .model_client import request_openai


def resolves_validation_failure(failure, kind, arguments, repair_for=None):
    """A successful unrelated import/query must never clear a failed scene edit."""
    if not failure or failure.get("tool") != kind:
        return False
    if failure.get("projectId") != arguments.get("projectId"):
        return False
    if repair_for and repair_for == failure.get("id"):
        return True
    if kind == "query_python" and failure.get("code") == arguments.get("code") and failure.get("code"):
        return True
    if kind not in ("run_python", "query_python"):
        return False
    def signature(code):
        try:
            tree = ast.parse(code or "")
            definitions = {node.name for node in tree.body
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            invoked = {node.func.id for statement in tree.body
                       if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                       for node in ast.walk(statement) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name) and node.func.id in definitions}
            return definitions, invoked
        except (SyntaxError, ValueError, TypeError):
            return set(), set()
    required, invoked = signature(failure.get("code"))
    repaired, executed = signature(arguments.get("code"))
    return bool(invoked) and required <= repaired and invoked <= executed


def record_validation_failure(job, failure):
    """Track every unresolved failure so repairing B cannot silently discard A."""
    failure = {"id": uuid4().hex, **failure}
    previous = job.get("unresolvedValidation", [])
    if isinstance(previous, dict):
        previous = [previous]
    job["unresolvedValidation"] = [*previous, failure]
    return failure


def resolve_validation_failures(job, kind, arguments, repair_for=None):
    previous = job.get("unresolvedValidation", [])
    if isinstance(previous, dict):
        previous = [previous]
    remaining = [failure for failure in previous
                 if not resolves_validation_failure(failure, kind, arguments, repair_for)]
    if remaining:
        job["unresolvedValidation"] = remaining
    else:
        job.pop("unresolvedValidation", None)


async def run(self, job, history, emit):
    await self._connect(job, emit)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=300, write=60, pool=30),
        transport=self.transport,
        follow_redirects=False,
    ) as http:

        async def request(path, body, *, stage_label=None):
            body = apply_presentation_rules(path, body, job.get("promptFilter", {}).get("generationRules", ""))
            return await request_openai(
                http, self.api_key, path, body, emit, stage_label=stage_label,
                usage_sink=lambda row: emit("ai_usage", row),
                job_id=job.get("id"), session_id=job.get("sessionId"),
            )

        prepared = await self._prepare_scene(request, job, history, emit)
        tools = prepared.tools
        messages = prepared.messages
        plan_instruction = prepared.instructions
        pending_calls = prepared.pending_calls
        composed_code = prepared.code
        verified_projects = set()
        version_projects = set()
        validation_failures = 0
        for _ in range(24):
            if job["reviewBudget"]["used"] >= SCENE_REVIEW_LIMIT:
                return await finalize_current_previs(self.client, job, history, emit)
            budget_instruction = (
                "\nInitial scene construction permits at most 3 validated remote build attempts, including confirmed failed attempts. "
                "Static validation rejections and pre-build reads do not consume the scene review budget. "
                "After the first successful scene build commit, scene inspection and correction share a maximum of 3 calls. "
                "Current review count: "
                + str(job["reviewBudget"]["used"])
                + "/3. Prioritize essential corrections. "
                "Rendering, operation completion polling and MP4 retrieval are not review calls; the backend finalizes the current scene when the review budget is exhausted."
            )
            if pending_calls:
                data = {"output": pending_calls}
                pending_calls = []
            else:
                data = await request(
                    "responses",
                    {
                        "model": job.get("chatModel", self.model),
                        "instructions": instructions(job, history)
                        + plan_instruction
                        + budget_instruction,
                        "input": messages,
                        "tools": tools,
                        "parallel_tool_calls": False,
                    },
                )
            output = data.get("output", [])
            messages.extend({key: value for key, value in item.items() if key != "validationRepairFor"}
                            for item in output)
            calls = [item for item in output if item.get("type") == "function_call"]
            if not calls:
                text = "\n".join(
                    content.get("text", "")
                    for item in output
                    for content in item.get("content", [])
                    if content.get("type") == "output_text"
                )
                if not text:
                    raise RuntimeError("AI 답변이 비어 있습니다.")
                if not any(media["mediaType"] == "video/mp4" for media in job["media"]):
                    emit("log", "[previs-repair] MP4 미생성 · AI가 남은 작업을 이어갑니다.")
                    messages.append(
                        {
                            "role": "user",
                            "content": "The requested MP4 is still missing. Continue the authorized scene repair/render/artifact retrieval using the existing project; do not end with a description or create duplicate projects.",
                        }
                    )
                    continue
                return {"message": text, "media": job["media"]}
            for call in calls:
                if job["reviewBudget"]["used"] >= SCENE_REVIEW_LIMIT:
                    return await finalize_current_previs(self.client, job, history, emit)
                if not tool_kind(call["name"]) or not any(
                    t["name"] == call["name"] for t in self.client.definitions
                ):
                    messages.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": json.dumps(
                                {
                                    "ok": False,
                                    "error": {
                                        "code": "TOOL_NOT_AVAILABLE",
                                        "message": "Choose an exact tool name from the provided definitions. No remote call was made.",
                                    },
                                }
                            ),
                        }
                    )
                    continue
                try:
                    arguments = json.loads(call["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                except (ValueError, TypeError):
                    messages.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": json.dumps(
                                {
                                    "ok": False,
                                    "error": {
                                        "code": "TOOL_ARGUMENTS_INVALID",
                                        "message": "Return a valid JSON object matching the tool schema. Correct the arguments and continue. No remote call was made.",
                                    },
                                }
                            ),
                        }
                    )
                    continue
                kind = tool_kind(call["name"])
                if kind == "preset_instructions" and not re.fullmatch(
                    r"/3d-jutsu(?:/references/[a-z0-9-]+)?", arguments.get("preset", "")
                ):
                    raise RuntimeError("3D Jutsu 범위 밖의 지침 조회는 허용되지 않습니다.")
                if kind in ("run_python", "query_python") and any(
                    record.get("projectId") == arguments.get("projectId")
                    and record.get("operationId") == arguments.get("operationId")
                    for record in job.get("repairHistory", [])
                ):
                    messages.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": json.dumps(
                                {
                                    "ok": False,
                                    "error": {
                                        "code": "REPAIR_REQUIRES_NEW_OPERATION",
                                        "message": "This operationId already ended in failure. Inspect settled scene guards and use a NEW operationId for repaired code. No remote request was sent.",
                                    },
                                }
                            ),
                        }
                    )
                    continue
                if (
                    kind in ("run_python", "import_asset")
                    and arguments.get("projectId") not in version_projects
                ):
                    messages.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": json.dumps(
                                {
                                    "ok": False,
                                    "error": {
                                        "code": "VERSION_PROJECT_REQUIRED",
                                        "message": "Create a NEW project for this version and rebuild the edited baseline code there. Earlier version projects must remain unchanged.",
                                    },
                                }
                            ),
                        }
                    )
                    emit(
                        "log", "이전 버전을 보존하기 위해 새 버전용 프로젝트 구성을 요청합니다."
                    )
                    continue
                if (
                    verified_projects
                    and kind not in ("create_project", "list_projects", "preset_instructions")
                    and arguments.get("projectId") not in verified_projects
                ):
                    feedback = {
                        "ok": False,
                        "error": {
                            "code": "UNVERIFIED_PROJECT_ID",
                            "message": "Use an exact projectId returned by a successful tool response. No remote request was made.",
                            "verifiedProjectIds": sorted(verified_projects),
                        },
                    }
                    emit(
                        "log",
                        "확인되지 않은 프로젝트 ID를 차단했습니다. AI가 실제 프로젝트 ID로 수정합니다.",
                    )
                    messages.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": json.dumps(feedback, ensure_ascii=False),
                        }
                    )
                    continue
                if kind == "run_python" and render_only_code(arguments.get("code")):
                    feedback = camera_quality_feedback(job, arguments.get("projectId"))
                    if feedback:
                        emit(
                            "log",
                            "[previs-camera] 카메라 품질 미달 · 렌더링 전에 카메라를 수정합니다.",
                        )
                        messages.append(
                            {
                                "type": "function_call_output",
                                "call_id": call["call_id"],
                                "output": json.dumps(
                                    {"ok": False, "error": feedback}, ensure_ascii=False
                                ),
                            }
                        )
                        continue
                deferred = False
                if kind in ("run_python", "query_python"):
                    validation_started = time.monotonic()
                    issues = validate_generated_code(
                        arguments.get("code"), forbidden_helpers=COMBINED_SCENE_CAMERA_HELPERS
                    )
                    if kind == "run_python" and not issues:
                        issues.extend(
                            validate_camera_integration(
                                arguments.get("code"),
                                job.get("workerDrafts", {}).get("camera", {}).get("code"),
                                allow_repair=job.get("cameraRepairAllowed", False)
                                or job.get("workerDrafts", {}).get("camera", {}).get("status")
                                == "needs_main_repair",
                            )
                        )
                    elapsed = time.monotonic() - validation_started
                    job.setdefault("codeValidation", []).append(
                        {
                            "operationId": arguments.get("operationId"),
                            "tool": kind,
                            "code": arguments.get("code"),
                            "issues": issues,
                            "passed": not issues,
                        }
                    )
                    emit("checkpoint", None)
                    emit("log", f"[previs-timing] 생성 코드 사전 검증: {elapsed:.2f}초")
                    if issues:
                        current_failure = record_validation_failure(job, {
                            "category": "execution_safety", "stage": "code_validation",
                            "tool": kind, "projectId": arguments.get("projectId"),
                            "code": arguments.get("code"),
                            "operationId": arguments.get("operationId"), "issues": issues,
                        })
                        emit("checkpoint", None)
                        validation_failures += 1
                        emit(
                            "log",
                            f"[previs-validation] 검증 실패 {validation_failures}회 · AI "
                            + ("수정" if validation_failures < 3 else "부분 재구성")
                            + " 요청 · "
                            + "; ".join(
                                f"{i['line']}행 {i['code']}: {i['message']}" for i in issues
                            ),
                        )
                        feedback = {
                            "ok": False,
                            "error": {
                                "code": "CODE_VALIDATION_FAILED",
                                "issues": issues,
                                "message": (
                                    "Fix the reported issues and resubmit. No remote request was made. "
                                    "If local edits keep failing, reconstruct only the problematic portion and supply missing implementation yourself. "
                                    "Preserve user action intent, contacts, event counts and camera requirements; do not disable validators or replace the requested action with a generic preset."
                                ),
                            },
                        }
                        messages.append(
                            {
                                "type": "function_call_output",
                                "call_id": call["call_id"],
                                "output": json.dumps(feedback, ensure_ascii=False),
                            }
                        )
                        if validation_failures <= 3:
                            patched = await repair_previs_functions(
                                request,
                                arguments.get("code"),
                                issues,
                                job.get("chatModel", self.model),
                            )
                            if patched is not None:
                                repaired_args = {
                                    **arguments,
                                    "code": patched,
                                    "operationId": "repair-functions-" + uuid4().hex[:16],
                                }
                                pending_calls = [
                                    {
                                        "type": "function_call",
                                        "name": call["name"],
                                        "call_id": "repair_" + uuid4().hex,
                                        "arguments": json.dumps(repaired_args),
                                        "validationRepairFor": current_failure["id"],
                                    }
                                ]
                                emit(
                                    "log",
                                    "[previs-repair] 문제 함수만 교체했습니다. 나머지 코드를 보존하고 재검증합니다.",
                                )
                        continue
                    emit("log", "[previs-validation] 통과 · 원격 실행을 진행합니다.")
                if kind == "run_python":
                    arguments["code"], deferred = split_scene_render(arguments["code"])
                    if deferred:
                        emit("log", "씬을 먼저 저장하고 MP4 출력은 별도 작업으로 진행합니다.")
                changes_scene = kind == "import_asset" or (
                    kind == "run_python" and not render_only_code(arguments.get("code"))
                )
                count_scene_review(job, kind, arguments.get("code"), emit)
                submitted_code = arguments.get("code")
                if kind in ("run_python", "query_python"):
                    if kind == "run_python":
                        job.setdefault("codeOperations", []).append(
                            {
                                "code": arguments["code"],
                                "operationId": arguments.get("operationId"),
                                "projectId": arguments.get("projectId"),
                                "status": "submitted",
                            }
                        )
                        emit("checkpoint", None)
                    arguments["code"] = prepare_cloud_python(
                        arguments["code"],
                        requirements=motion_requirements(
                            history,
                            detailed_motion=(job.get("plan") or {}).get("motion_detail")
                            == "requested_detail",
                        ),
                    )
                    if kind == "run_python" and changes_scene:
                        arguments["code"] += "\n" + CAMERA_QUALITY_REPORT_CODE
                if changes_scene:
                    # A later mutation must not return the MP4 from the earlier scene.
                    if job.get("media"):
                        job["media"] = []
                        emit("checkpoint", None)
                        emit(
                            "log",
                            "[previs-finalize] 장면 수정으로 이전 결과를 무효화하고 최신 영상을 생성합니다.",
                        )
                emit(
                    "payload",
                    {
                        "endpoint": self.client.status["endpoint"],
                        "tool": call["name"],
                        "arguments": arguments,
                    },
                )
                emit("log", "3D Jutsu: " + kind)
                tool_started = time.monotonic()
                try:
                    result = await call_tool(self.client, call["name"], arguments, emit)
                except Exception as error:
                    response = getattr(error, "tool_response", None)
                    if kind in ("run_python", "query_python") and repairable_operation_failure(
                        result_data(response or {})
                    ):
                        result = response
                    else:
                        if kind == "run_python" and job.get("codeOperations"):
                            job["codeOperations"][-1]["status"] = "unknown"
                            emit("checkpoint", None)
                        emit("response", response or {"error": str(error)})
                        raise
                finally:
                    emit(
                        "log",
                        f"[previs-timing] 원격 도구 {kind} 최초 응답: {time.monotonic() - tool_started:.2f}초",
                    )
                emit("response", result)
                result = await settle_operation(self.client, result, emit)
                content = result_data(result)
                if kind == "run_python" and job.get("codeOperations"):
                    job["codeOperations"][-1]["status"] = (
                        "failed"
                        if isinstance(content, dict)
                        and (
                            content.get("ok") is False
                            or content.get("operation", {}).get("status")
                            in ("failed", "timed_out", "expired")
                        )
                        else "succeeded"
                    )
                    emit("checkpoint", None)
                if (isinstance(content, dict) and content.get("ok") is not False
                        and content.get("operation", {}).get("status") == "succeeded"):
                    resolve_validation_failures(job, kind, {**arguments, "code": submitted_code},
                                                call.get("validationRepairFor"))
                    emit("checkpoint", None)
                if isinstance(content, dict):
                    capture_spatial_manifest(job, content, self.client.definitions)
                    stdout = (content.get("operation", {}).get("output") or {}).get(
                        "stdoutTail", ""
                    )
                    if isinstance(stdout, str):
                        for scale_line in stdout.splitlines():
                            if scale_line.startswith(("PREVIS_CAMERA_SCALE ", "{")):
                                try:
                                    legacy = scale_line.startswith("PREVIS_CAMERA_SCALE ")
                                    scale_report = json.loads(
                                        scale_line.split(" ", 1)[1] if legacy else scale_line
                                    )
                                    if not isinstance(scale_report, dict):
                                        continue
                                    if (
                                        legacy
                                        or scale_report.get("event") == "previs_camera_scale"
                                    ):
                                        job["cameraScale"] = scale_report
                                    elif scale_report.get(
                                        "event"
                                    ) == "camera_check" and isinstance(
                                        scale_report.get("revised"), dict
                                    ):
                                        job["cameraMetrics"] = scale_report["revised"]
                                    elif (
                                        scale_report.get("event") == "previs_camera_quality"
                                        and content.get("operation", {}).get("status")
                                        == "succeeded"
                                        and isinstance(scale_report.get("issues"), list)
                                    ):
                                        job["cameraQuality"] = {
                                            "projectId": arguments.get("projectId"),
                                            "issues": scale_report["issues"],
                                        }
                                        if scale_report["issues"]:
                                            job["cameraRepairAllowed"] = True
                                    else:
                                        continue
                                    emit("checkpoint", None)
                                    emit(
                                        "log",
                                        "[previs-camera] "
                                        + json.dumps(scale_report, ensure_ascii=False),
                                    )
                                except ValueError:
                                    pass
                        for phase, seconds in re.findall(
                            r"JUTSU_RENDER (validation_complete|render_complete) seconds=([0-9]+(?:\.[0-9]+)?)",
                            stdout,
                        ):
                            label = (
                                "원격 장면 검증"
                                if phase == "validation_complete"
                                else "원격 영상 렌더링"
                            )
                            emit("log", f"[previs-timing] {label}: {seconds}초")
                    if content.get("ok") is False or content.get("operation", {}).get(
                        "status"
                    ) in ("failed", "timed_out", "expired"):
                        raw = (
                            content.get("error")
                            or content.get("operation", {}).get("error")
                            or content
                        )
                        if kind in ("run_python", "query_python"):
                            current_failure = record_validation_failure(job, {
                                "category": "execution_safety", "stage": "remote_execution",
                                "tool": kind, "projectId": arguments.get("projectId"),
                                "code": next((item["code"] for item in reversed(job.get("codeOperations", []))
                                    if item.get("operationId") == arguments.get("operationId")), submitted_code),
                                "operationId": arguments.get("operationId"), "error": raw,
                            })
                            emit("checkpoint", None)
                        if kind in (
                            "run_python",
                            "query_python",
                        ) and repairable_operation_failure(content):
                            operation = content["operation"]
                            job.setdefault("repairHistory", []).append(
                                {
                                    "operationId": operation.get("id"),
                                    "projectId": arguments.get("projectId"),
                                    "error": raw,
                                    "output": operation.get("output"),
                                    "strategy": (
                                        "repair"
                                        if len(job.get("repairHistory", [])) < 2
                                        else "reconstruct_missing_parts"
                                    ),
                                }
                            )
                            failure_text = json.dumps(content, ensure_ascii=False)
                            if "build_camera" in failure_text or "Camera" in failure_text:
                                job["cameraRepairAllowed"] = True
                            emit("checkpoint", None)
                            emit(
                                "log",
                                "[previs-repair] 원격 코드·검증 문제 발견 · AI가 원인 수정과 부족 부분 보완 후 재검증합니다.",
                            )
                            if (
                                not job["reviewBudget"].get("buildCommitted")
                                and job["reviewBudget"].get("initialBuildAttempts", 0)
                                >= INITIAL_BUILD_LIMIT
                            ):
                                job["finalization"] = {
                                    "state": "build_failed",
                                    "projectId": arguments.get("projectId"),
                                    "operationId": operation.get("id"),
                                    "error": raw,
                                }
                                emit("checkpoint", None)
                                raise RuntimeError(
                                    "초기 장면 생성 3회 모두 오류로 종료되었습니다. 마지막 원인: "
                                    + json.dumps(raw, ensure_ascii=False)
                                )
                            feedback = {
                                "toolResponse": result,
                                "error": {
                                    "code": "AI_REPAIR_REQUIRED",
                                    "message": "This operation has ended with a confirmed failure. Read the exact stderr and failed source already in this conversation. Inspect the current settled project/revision/sceneSequence before editing. Use a NEW operationId for changed code. Repair the cause; if patching is insufficient, reconstruct the deficient portion yourself while preserving user intent and existing valid scene/camera. Do not add unrelated choreography, disable validation or invoke fixed motion presets. On timeout reduce/split work. Then validate, render and retrieve MP4; do not terminate with an error explanation.",
                                },
                            }
                            messages.append(
                                {
                                    "type": "function_call_output",
                                    "call_id": call["call_id"],
                                    "output": json.dumps(feedback, ensure_ascii=False),
                                }
                            )
                            source_record = next(
                                (
                                    item
                                    for item in reversed(job.get("codeOperations", []))
                                    if item.get("projectId") == arguments.get("projectId")
                                    and not render_only_code(item.get("code"))
                                ),
                                None,
                            )
                            if kind == "run_python" and source_record:
                                patched = await repair_previs_functions(
                                    request,
                                    source_record["code"],
                                    [],
                                    job.get("chatModel", self.model),
                                    failure_text=failure_text,
                                )
                                revision = operation.get("headRevision")
                                sequence = operation.get("headSceneSequence")
                                if (
                                    patched is not None
                                    and type(revision) is int
                                    and type(sequence) is int
                                ):
                                    repaired_args = {
                                        **arguments,
                                        "code": patched,
                                        "revision": revision,
                                        "expectedSceneSequence": sequence,
                                        "operationId": "repair-functions-" + uuid4().hex[:16],
                                    }
                                    pending_calls = [
                                        {
                                            "type": "function_call",
                                            "name": call["name"],
                                            "call_id": "repair_" + uuid4().hex,
                                            "arguments": json.dumps(repaired_args),
                                        "validationRepairFor": current_failure["id"],
                                        }
                                    ]
                                    emit(
                                        "log",
                                        "[previs-repair] 런타임 오류 함수만 교체하고 새 작업으로 재검증합니다.",
                                    )
                            continue
                        raise RuntimeError(
                            raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                        )
                    if (
                        kind == "run_python"
                        and changes_scene
                        and content.get("operation", {}).get("status") == "succeeded"
                    ):
                        job["reviewBudget"]["buildCommitted"] = True
                        emit("checkpoint", None)
                    if content.get("ok") is True:
                        project = content.get("project") or {}
                        project_id = (
                            content.get("projectId")
                            or project.get("id")
                            or project.get("projectId")
                            or content.get("operation", {}).get("projectId")
                        )
                        if isinstance(project_id, str) and project_id:
                            verified_projects.add(project_id)
                            if kind == "create_project":
                                version_projects.add(project_id)
                            projects = job.setdefault("remoteProjects", [])
                            if project_id not in projects:
                                projects.append(project_id)
                            emit("checkpoint", None)
                    if (
                        kind in ("get_artifact", "get_blend")
                        and content.get("ok")
                        and content.get("download", {}).get("method") == "GET"
                    ):
                        media_type = content.get("artifact", {}).get("mediaType") or (
                            "application/x-blender" if kind == "get_blend" else None
                        )
                        if media_type in ("video/mp4", "application/x-blender"):
                            job["media"].append(
                                {
                                    "name": content.get("artifact", {}).get("name")
                                    or "previs.blend",
                                    "mediaType": media_type,
                                    "download": content["download"],
                                    "artifactRef": {
                                        "tool": call["name"],
                                        "arguments": arguments,
                                    },
                                }
                            )
                if kind in ("run_python", "import_asset"):
                    job["mutated"] = True
                feedback = (
                    camera_quality_feedback(job, arguments.get("projectId"))
                    if changes_scene
                    else None
                )
                if feedback:
                    forwarded = {
                        "toolResponse": result,
                        "cameraQuality": feedback,
                        "nextStep": feedback["message"],
                    }
                elif (
                    kind == "run_python"
                    and changes_scene
                    and isinstance(content, dict)
                    and content.get("ok") is True
                    and content.get("operation", {}).get("status") == "succeeded"
                ):
                    emit("log", "[previs-finalize] 장면 생성·검증 완료 · 바로 렌더링합니다.")
                    try:
                        return await finalize_current_previs(
                            self.client,
                            job,
                            history,
                            emit,
                            settled_operation=content["operation"],
                        )
                    except RuntimeError as error:
                        failure = getattr(error, "tool_response", None)
                        failed_content = (
                            result_data(failure)
                            if isinstance(failure, dict) and "operation" not in failure
                            else failure
                        )
                        if not isinstance(
                            failed_content, dict
                        ) or not repairable_operation_failure(failed_content):
                            raise
                        forwarded = {
                            "toolResponse": failed_content,
                            "nextStep": "Rendering found a confirmed code or validation issue. Repair only the affected functions using the existing scene and a new operationId, then render again.",
                        }
                elif deferred:
                    forwarded = {
                        "toolResponse": result,
                        "nextStep": (
                            "Scene build committed without rendering. Use revisionAfter and sceneSequenceAfter "
                            "with a new operationId and code: result=render_previs()."
                        ),
                    }
                else:
                    forwarded = result
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(forwarded, ensure_ascii=False),
                    }
                )
                if (
                    composed_code
                    and job["composition"]["state"] == "ready"
                    and kind == "create_project"
                ):
                    if (
                        not isinstance(content, dict)
                        or content.get("ok") is not True
                        or not project_id
                    ):
                        raise RuntimeError("새 프리비즈 프로젝트 식별자를 확인할 수 없습니다.")
                    project_tool = next(
                        tool
                        for tool in self.client.definitions
                        if tool_kind(tool["name"]) == "get_project"
                    )
                    pending_calls = [
                        {
                            "type": "function_call",
                            "name": project_tool["name"],
                            "call_id": "compose_" + uuid4().hex,
                            "arguments": json.dumps({"projectId": project_id}),
                        }
                    ]
                    job["composition"].update(state="checking", projectId=project_id)
                elif (
                    composed_code
                    and job["composition"]["state"] == "checking"
                    and kind == "get_project"
                ):
                    project = content.get("project") or content
                    revision, sequence = project.get("revision"), project.get("sceneSequence")
                    if (
                        content.get("ok") is not True
                        or type(revision) is not int
                        or type(sequence) is not int
                        or project.get("activeOperationId")
                    ):
                        raise RuntimeError(
                            "새 프로젝트의 리비전과 실행 상태를 확인할 수 없습니다."
                        )
                    build_tool = next(
                        tool
                        for tool in self.client.definitions
                        if tool_kind(tool["name"]) == "run_python"
                    )
                    arguments = {
                        "projectId": job["composition"]["projectId"],
                        "operationId": "build-previs-" + uuid4().hex[:16],
                        "revision": revision,
                        "expectedSceneSequence": sequence,
                        "waitSeconds": 8,
                        "code": composed_code,
                    }
                    pending_calls = [
                        {
                            "type": "function_call",
                            "name": build_tool["name"],
                            "call_id": "compose_" + uuid4().hex,
                            "arguments": json.dumps(arguments),
                        }
                    ]
                    job["composition"]["state"] = "building"
                    emit(
                        "log",
                        "[previs-compose] 리비전 확인 완료 · 조합한 장면 코드를 실행합니다.",
                    )
                emit("checkpoint", None)
    # Artifact retrieval can consume the final tool turn. A confirmed MP4
    # is complete even when there is no budget for one more prose response.
    if any(media.get("mediaType") == "video/mp4" for media in job["media"]):
        return {"message": "프리비즈 MP4 생성이 완료되었습니다.", "media": job["media"]}
    return await finalize_current_previs(self.client, job, history, emit)

