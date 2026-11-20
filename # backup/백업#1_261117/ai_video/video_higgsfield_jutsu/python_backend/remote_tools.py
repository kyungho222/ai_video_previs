"""Remote previs tool calls and bounded read-only operation recovery."""

import asyncio
import copy
import json
import re
import time

import httpx




def tool_kind(name):
    if re.search(r"(?:^|_)get_preset_instructions$", name):
        return "preset_instructions"
    match = re.search(
        r"(?:^|_)scene_builder_3d_(create_project|list_projects|get_project|query_python|run_python|get_operation|get_artifact|get_blend|get_glb|search_assets|import_asset|show_scene)$",
        name,
    )
    return match[1] if match else None


def result_data(result):
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for content in result.get("content", []):
        if content.get("type") == "text":
            try:
                return json.loads(content.get("text", ""))
            except ValueError:
                pass
    return None


async def load_workflow_instructions(client, emit):
    tool = next(
        (t for t in client.definitions if tool_kind(t["name"]) == "preset_instructions"), None
    )
    if tool is None:
        raise RuntimeError(
            "필수 get_preset_instructions 도구가 없습니다. 프로젝트를 생성하지 않았습니다. 도구 연결·허용목록을 확인하세요."
        )
    documents = {}
    for preset in (
        "/3d-jutsu",
        "/3d-jutsu/references/scene-building",
        "/3d-jutsu/references/animation-audit",
    ):
        arguments = {"preset": preset}
        emit(
            "payload",
            {"endpoint": client.status["endpoint"], "tool": tool["name"], "arguments": arguments},
        )
        response = await call_tool(client, tool["name"], arguments, emit)
        emit("response", response)
        data = result_data(response)
        if isinstance(data, dict) and (
            data.get("mode") not in (None, "instructions", "preset") or data.get("ok") is False
        ):
            raise RuntimeError(
                "필수 워크플로 지침 조회 실패: "
                + preset
                + " · "
                + json.dumps(data, ensure_ascii=False)
            )
        text = data.get("instructions_markdown", "") if isinstance(data, dict) else ""
        if not text:
            text = "\n".join(
                item.get("text", "")
                for item in response.get("content", [])
                if item.get("type") == "text"
            )
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("필수 워크플로 지침이 비어 있습니다: " + preset)
        documents[preset] = text
        emit("log", "워크플로 지침 로드: " + preset)
    return documents


def repairable_operation_failure(data):
    if not isinstance(data, dict):
        return False
    operation = data.get("operation") or {}
    error = data.get("error") or operation.get("error") or {}
    return (
        operation.get("status") in ("failed", "timed_out", "expired")
        and isinstance(error, dict)
        and error.get("code") in ("PYTHON_EXCEPTION", "EXECUTION_TIMEOUT")
    )


async def call_tool(client, name, arguments, emit=None, *, sleep=asyncio.sleep):
    original_arguments = copy.deepcopy(arguments)
    read_only = tool_kind(name) in (
        "get_operation",
        "get_project",
        "list_projects",
        "get_artifact",
        "get_blend",
        "get_glb",
        "search_assets",
        "show_scene",
        "preset_instructions",
    )
    for attempt in range(3):
        try:
            result = await client.call(name, copy.deepcopy(original_arguments))
        except (TimeoutError, ConnectionError, httpx.RequestError, RuntimeError):
            if not read_only or attempt == 2:
                raise
            if emit:
                emit(
                    "log",
                    "[previs-recovery] 상태·자료 조회 일시 오류 · 같은 조회를 다시 확인합니다.",
                )
            await sleep(attempt + 1)
            continue
        data = result_data(result)
        if data is None and result.get("ok") is False:
            data = result
        detail = data.get("error") if isinstance(data, dict) else None
        temporary_authorization_error = (
            isinstance(detail, dict)
            and detail.get("code") == "PROJECT_AUTHORIZATION_UNAVAILABLE"
            and detail.get("retryable") is True
            and detail.get("status") == 503
            and not data.get("operation")
            and not data.get("artifacts")
            and not data.get("generatedArtifacts")
        )
        if not temporary_authorization_error or attempt == 2:
            break
        if emit:
            emit("log", f"프로젝트 권한 확인 일시 오류 · {attempt+2}/3차 재시도 준비")
        await sleep(attempt + 1)
    if temporary_authorization_error:
        error = RuntimeError(
            "PROJECT_AUTHORIZATION_UNAVAILABLE: 프로젝트 권한 확인이 3회 실패했습니다. 잠시 후 다시 시도하세요."
        )
        error.tool_response = result
        raise error
    if result.get("isError"):
        if repairable_operation_failure(data):
            return result
        error = RuntimeError(
            "\n".join(
                item.get("text", "")
                for item in result.get("content", [])
                if item.get("type") == "text"
            )
            or "3D Jutsu 도구 호출 오류"
        )
        error.tool_response = result
        raise error
    return result


async def settle_operation(client, response, emit, *, sleep=asyncio.sleep, now=time.monotonic):
    started = now()
    data = result_data(response)

    def terminal(value):
        return value.get("terminal") or value.get("operation", {}).get("status") in (
            "succeeded",
            "failed",
            "timed_out",
            "expired",
        )

    if not isinstance(data, dict) or not data.get("operation") or terminal(data):
        return response
    tool = next((t for t in client.definitions if tool_kind(t["name"]) == "get_operation"), None)
    if not tool:
        raise RuntimeError("작업 상태 조회 도구가 없습니다.")
    arguments = {
        "projectId": data["operation"]["projectId"],
        "operationId": data["operation"]["id"],
    }
    polls = 0
    while not terminal(data):
        if now() - started > 330:
            raise RuntimeError(
                "원격 작업의 종료 상태를 확인하지 못했습니다. 동일 편집을 재실행하지 말고 operationId="
                + arguments["operationId"]
                + " 상태를 확인하세요."
            )
        await sleep(2)
        emit(
            "payload",
            {"endpoint": client.status["endpoint"], "tool": tool["name"], "arguments": arguments},
        )
        try:
            response = await call_tool(client, tool["name"], arguments, emit, sleep=sleep)
        except (TimeoutError, ConnectionError, httpx.RequestError, RuntimeError) as error:
            failed_response = getattr(error, "tool_response", None)
            failed_data = result_data(failed_response) if isinstance(failed_response, dict) else None
            if isinstance(failed_data, dict) and failed_data.get("operation") and terminal(failed_data):
                response, data = failed_response, failed_data
                break
            emit(
                "log",
                "[previs-recovery] 원격 작업 상태 조회가 지연되었습니다. 편집을 재실행하지 않고 완료 상태를 계속 확인합니다.",
            )
            continue
        polls += 1
        emit("response", response)
        candidate = result_data(response)
        if not isinstance(candidate, dict) or not candidate.get("operation"):
            emit("log", "[previs-recovery] 불완전한 상태 응답 · 이전 작업 ID로 다시 조회합니다.")
            continue
        data = candidate
    emit(
        "log", f"[previs-timing] 원격 작업 완료 대기: {now() - started:.2f}초 (상태 조회 {polls}회)"
    )
    return response

