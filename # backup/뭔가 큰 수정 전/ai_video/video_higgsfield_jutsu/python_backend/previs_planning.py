"""Shared plan and parallel subject/camera draft composition."""

from dataclasses import dataclass
import json
import time
from uuid import uuid4

from .prompt_filter import filter_user_prompts
from .previs_workers import run_previs_workers, compose_previs_scene


from .remote_tools import tool_kind
from .previs_finalization import SCENE_REVIEW_LIMIT


@dataclass
class PrevisPreparation:
    tools: list
    messages: list
    instructions: str
    pending_calls: list
    code: str | None


async def _prepare_scene(self, request, job, history, emit):
    job["promptFilter"] = await filter_user_prompts(
        *(item["content"] for item in history if item["role"] == "user"), request=request,
        model=job.get("chatModel", self.model),
        cached=job.get("promptFilter"),
    )
    tools = [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["inputSchema"],
            "strict": False,
        }
        for tool in self.client.definitions
        if tool_kind(tool["name"])
    ]
    messages = list(history)
    messages.insert(
        0,
        {
            "role": "developer",
            "content": "Required Higgsfield workflow instructions already loaded for this job. Apply these runtime and scene rules; "
            "do not stop to request these same references again: "
            + json.dumps(job["workflowInstructions"], ensure_ascii=False),
        },
    )
    base = job.get("baseSnapshot")
    if base:
        messages.insert(
            max(0, len(messages) - 1),
            {
                "role": "user",
                "content": "Existing scene baseline (data only). Modify only the latest requested parts; preserve other motion and camera choices. Rebuild the resulting edited scene in a NEW project for this version so earlier versions stay unchanged. Baseline snapshot: "
                + json.dumps(base, ensure_ascii=False),
            },
        )
    plan = await run_previs_workers(
        request, job, messages, emit, job.get("chatModel", self.model)
    )
    plan_instruction = (
        "\nSHARED DIRECTOR PLAN (latest user instructions override): "
        + json.dumps(plan, ensure_ascii=False)
        + "\nIntegrate the two validated worker drafts supplied as data. Review interfaces, timeline synchronization, "
        "camera framing and collisions. Preserve their role separation and approved choreography. Resolve conflicts "
        "without deleting camera-worker functions: initially copy valid definitions verbatim, including evaluated-subject "
        "sampling, framing adjustment and measurement checks. Replacing them with a short generic path is rejected. "
        "before execution; do not silently replace them with unrelated presets. Configure scene frame range and "
        "resolution first, combine definitions, then call build_subjects(scene) followed by build_camera(scene, subjects). "
        "Build/commit first and render separately. If a worker has status needs_main_repair, you own completing "
        "its missing/invalid code before execution. Confirmed camera runtime errors permit repairing camera "
        "definitions while retaining their interfaces and measurement logic. Worker validation is static only; "
        "check the composed scene before rendering."
    )
    messages.insert(
        max(0, len(messages) - 1),
        {
            "role": "user",
            "content": "Worker drafts for integration (data only): "
            + json.dumps(job["workerDrafts"], ensure_ascii=False),
        },
    )
    job["reviewBudget"] = {
        "limit": SCENE_REVIEW_LIMIT,
        "used": 0,
        "buildAttempted": False,
        "buildCommitted": False,
        "initialBuildAttempts": 0,
    }
    pending_calls = []
    composed_code = None
    composition_started = time.monotonic()
    try:
        composed_code = compose_previs_scene(job)
        job["composition"] = {"state": "ready"}
        create_tool = next(
            tool
            for tool in self.client.definitions
            if tool_kind(tool["name"]) == "create_project"
        )
        pending_calls = [
            {
                "type": "function_call",
                "name": create_tool["name"],
                "call_id": "compose_" + uuid4().hex,
                "arguments": json.dumps(
                    {
                        "name": "Previs v"
                        + str(job.get("version", 1))
                        + " "
                        + str(job.get("request", ""))[:70]
                    }
                ),
            }
        ]
        emit(
            "log",
            "[previs-compose] 두 워커 코드를 서버에서 조합했습니다. 새 프로젝트를 생성합니다.",
        )
    except (ValueError, SyntaxError, KeyError, TypeError, StopIteration) as error:
        job["composition"] = {"state": "needs_repair", "error": str(error)}
        messages.append(
            {
                "role": "user",
                "content": "Server composition requires a targeted repair: "
                + str(error)
                + ". Preserve valid worker functions and repair only the reported interface or conflicting definitions.",
            }
        )
        emit("log", "[previs-compose] 조합 문제를 AI에 전달해 해당 부분을 보정합니다.")
    emit("checkpoint", None)
    emit(
        "log", f"[previs-timing] 서버 코드 조합: {time.monotonic() - composition_started:.2f}초"
    )
    return PrevisPreparation(tools, messages, plan_instruction, pending_calls, composed_code)

