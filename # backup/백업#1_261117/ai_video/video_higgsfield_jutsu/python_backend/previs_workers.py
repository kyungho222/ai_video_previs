"""Director planning and two independent, cooperating code-generation workers."""

import ast
import asyncio
import json
import math
import re

from .cloud_python import (
    CAMERA_REVISION_POLICY,
    COMBINED_SCENE_CAMERA_HELPERS,
    NATURAL_PREVIS_POLICY,
    CAMERA_FOUR_ELEMENT_POLICY,
)
from .code_validation import validate_generated_code, apply_function_repairs, JSON_LOG_POLICY, previs_api_contract

WORKER_DRAFT_ATTEMPTS = 3


async def repair_previs_functions(
    request, code, issues, model, *, failure_text="", stage_label="문제 함수 보정"
):
    """Ask for affected functions only, then splice them into the original module."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError):
        return None
    functions = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    error_lines = {issue.get("line") for issue in issues if isinstance(issue.get("line"), int)}
    targets = [
        node
        for node in functions
        if any(node.lineno <= line <= node.end_lineno for line in error_lines)
        or re.search(r"\bin\s+" + re.escape(node.name) + r"\b", failure_text)
    ]
    if not targets:
        return None
    context = {
        "apiContract": json.loads(previs_api_contract()),
        "issues": issues,
        "failure": failure_text,
        "functions": {
            node.name: "\n".join(
                code.splitlines()[
                    min([node.lineno, *[item.lineno for item in node.decorator_list]])
                    - 1 : node.end_lineno
                ]
            )
            for node in targets
        },
        "interfaces": [node.name + "(" + ast.unparse(node.args) + ")" for node in functions],
        "moduleSetup": "\n".join(
            ast.get_source_segment(code, node)
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))
        ),
    }
    response = await request(
        "responses",
        {
            "model": model,
            "instructions": "Repair only the affected Python functions. Return complete replacement definitions for those functions. "
            "Preserve function interfaces, the requested scene, animation, camera sampling and validation contracts. "
            "Use existing module globals and helper interfaces. Keep validation enabled. Return replacements as structured JSON.\n"
            + NATURAL_PREVIS_POLICY
            + "\nPREVIS_API_CONTRACT\n" + previs_api_contract(),
            "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "previs_function_repairs",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "replacements": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "code": {"type": "string"},
                                    },
                                    "required": ["name", "code"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["replacements"],
                        "additionalProperties": False,
                    },
                }
            },
        },
        stage_label=stage_label,
    )
    try:
        data = read_json(response)
        return apply_function_repairs(code, data["replacements"], {node.name for node in targets})
    except (SyntaxError, ValueError, KeyError, TypeError):
        return None


PROMPT_SECTIONS = {
    "scene": "SCENE",
    "choreography": "CHOREOGRAPHY",
    "motion": "MOTION",
    "camera": "CAMERA",
    "mood_style": "MOOD / STYLE",
}
PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        section: {"type": "array", "items": {"type": "string"}} for section in PROMPT_SECTIONS
    },
    "required": list(PROMPT_SECTIONS),
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "preprocessed_prompt": PROMPT_SCHEMA,
        "motion_detail": {
            "type": "string",
            "enum": ["basic_motion", "blocking", "requested_detail"],
        },
        "scene_action": {"type": "string"},
        "camera": {"type": "string"},
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "subject_action": {"type": "string"},
                    "camera_goal": {"type": "string"},
                },
                "required": ["start", "end", "subject_action", "camera_goal"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["preprocessed_prompt", "motion_detail", "scene_action", "camera", "timeline"],
    "additionalProperties": False,
}
CODE_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string"}, "notes": {"type": "string"}},
    "required": ["code", "notes"],
    "additionalProperties": False,
}


def read_json(response):
    text = "".join(
        part.get("text", "")
        for item in response.get("output", [])
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    return json.loads(text)


def format_preprocessed_prompt(sections):
    if not isinstance(sections, dict) or set(sections) != set(PROMPT_SECTIONS):
        raise ValueError("Provide all five prompt sections")
    blocks = []
    for key, heading in PROMPT_SECTIONS.items():
        bullets = sections[key]
        if not isinstance(bullets, list) or any(
            not isinstance(bullet, str) or not bullet.strip() for bullet in bullets
        ):
            raise ValueError(f"{heading} must contain concise bullet strings")
        if not bullets and key != "choreography":
            raise ValueError(f"{heading} must contain at least one bullet")
        lines = [f'- {" ".join(bullet.split())}' for bullet in bullets]
        if not lines:
            lines = ["- 해당 없음"]
        blocks.append(f"### {heading}\n\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def validate_plan(plan, duration, scenario_count=None):
    if not isinstance(plan, dict) or not all(
        isinstance(plan.get(k), str) and plan[k].strip() for k in ("scene_action", "camera")
    ):
        raise ValueError("Scene action and camera briefs are required")
    if plan.get("motion_detail") not in ("basic_motion", "blocking", "requested_detail"):
        raise ValueError("Choose basic_motion, blocking or requested_detail for previs motion")
    timeline = plan.get("timeline")
    if not isinstance(timeline, list) or not timeline or len(timeline) > 40:
        raise ValueError("Provide a shared timeline with 1 to 40 intervals")
    if scenario_count is not None and len(timeline) != scenario_count:
        raise ValueError(f"Provide exactly {scenario_count} timeline intervals, one per numbered scenario scene in order")
    previous = 0.0
    for event in timeline:
        if not isinstance(event, dict):
            raise ValueError("Invalid timeline event")
        start, end = event.get("start"), event.get("end")
        if (
            type(start) not in (int, float)
            or type(end) not in (int, float)
            or not math.isfinite(start)
            or not math.isfinite(end)
            or abs(start - previous) > 0.001
            or end <= start
            or end > duration + 0.001
        ):
            raise ValueError(
                "Timeline must cover the shot continuously from zero to requested duration"
            )
        if not all(
            isinstance(event.get(k), str) and event[k].strip()
            for k in ("subject_action", "camera_goal")
        ):
            raise ValueError("Each interval must link subject action to camera intent")
        previous = end
    if abs(previous - duration) > 0.001:
        raise ValueError("Timeline must end at requested duration")


def validate_worker(code, role):
    issues = validate_generated_code(code, forbidden_helpers=COMBINED_SCENE_CAMERA_HELPERS)
    if issues:
        return issues
    tree = ast.parse(code)
    entry = "build_subjects" if role == "subjects" else "build_camera"
    args = ["scene"] if role == "subjects" else ["scene", "subjects"]
    function = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == entry), None
    )
    if function is None or [a.arg for a in function.args.args] != args:
        issues.append(
            {
                "code": "WORKER_INTERFACE",
                "line": 1,
                "message": f'Define {entry}({", ".join(args)}). All scene changes must be inside functions.',
            }
        )
    if role == "camera" and not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "previs_camera_from_scale"
        for n in ast.walk(tree)
    ):
        issues.append(
            {
                "code": "CAMERA_SCALE_REQUIRED",
                "line": 1,
                "message": "Use previs_camera_from_scale for actual motion-envelope scaling. For explicit absolute/fixed camera use all scale_axes=False and fit_frame=False.",
            }
        )
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            try:
                ast.literal_eval(node.value)
                continue
            except (ValueError, TypeError):
                pass
        issues.append(
            {
                "code": "WORKER_SIDE_EFFECT",
                "line": node.lineno,
                "message": "Return function definitions and literal constants only; no top-level scene execution.",
            }
        )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if role == "subjects" and (
            "camera_add" in name
            or name in ("previs_camera_smooth", "previs_orbit_camera", "build_camera")
        ):
            issues.append(
                {
                    "code": "WORKER_ROLE",
                    "line": node.lineno,
                    "message": "Subject worker must not create or animate cameras.",
                }
            )
        if role == "camera" and (
            name.startswith("bpy.ops.mesh.")
            or name in ("previs_humanoid", "build_subjects", "previs_register_motion")
        ):
            issues.append(
                {
                    "code": "WORKER_ROLE",
                    "line": node.lineno,
                    "message": "Camera worker must not construct or animate subjects.",
                }
            )
    return issues


def _camera_behavior_ast(node):
    """Compare camera behavior while ignoring side-effect-free JSON diagnostics."""

    class Diagnostics(ast.NodeTransformer):
        def visit_Expr(self, expression):
            call = expression.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "print"
                and len(call.args) == 1
            ):
                payload = call.args[0]
                if (
                    isinstance(payload, ast.Call)
                    and ast.unparse(payload.func) == "json.dumps"
                    and all(
                        ast.unparse(n.func) in ("print", "json.dumps", "dict")
                        for n in ast.walk(call)
                        if isinstance(n, ast.Call)
                    )
                    and not any(
                        isinstance(n, (ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom))
                        for n in ast.walk(call)
                    )
                ):
                    return ast.Expr(value=ast.Constant(value="camera_json_diagnostic"))
            return expression

    # Parse a copy: validation must not alter the caller's submitted tree.
    return ast.dump(Diagnostics().visit(ast.parse(ast.unparse(node))))


def validate_camera_integration(code, camera_draft, *, allow_repair=False):
    """Catch the integrator replacing the camera specialist with a simpler implementation.

    This checks source preservation, not visual quality or all possible runtime overrides.
    Render-only and unrelated query operations do not define the scene entry points.
    """
    if not camera_draft:
        return []
    try:
        tree = ast.parse(code)
        draft_tree = ast.parse(camera_draft)
    except (SyntaxError, TypeError):
        return []  # The ordinary syntax validator reports this.
    functions = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            functions.setdefault(node.name, []).append(node)
    if not {"build_subjects", "build_camera"}.intersection(functions):
        return []
    issues = []
    for node in draft_tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        actual = functions.get(node.name, [])
        if len(actual) != 1 or (
            not allow_repair and _camera_behavior_ast(actual[0]) != _camera_behavior_ast(node)
        ):
            issues.append(
                {
                    "code": "CAMERA_WORKER_DISCARDED",
                    "line": actual[0].lineno if actual else 1,
                    "message": f"Preserve camera worker function {node.name} verbatim, including its geometry sampling, framing and measurement logic. Compose the worker draft; do not replace it with a simplified path.",
                }
            )
    return issues


def compose_previs_scene(job):
    """Connect fresh worker definitions using the shared scene interface."""
    sources = []
    bindings = {}
    for role in ("subjects", "camera"):
        code = job["workerDrafts"][role]["code"]
        issues = validate_worker(code, role)
        if issues:
            raise ValueError(role + ": " + json.dumps(issues, ensure_ascii=False))
        for node in ast.parse(code).body:
            names = []
            if isinstance(node, ast.FunctionDef):
                names = [(node.name, ast.dump(node))]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [
                    (item.id, ast.dump(node.value))
                    for target in targets
                    for item in ast.walk(target)
                    if isinstance(item, ast.Name)
                ]
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    raise ValueError("Worker imports must be executable after scene setup")
                names = [
                    (
                        item.asname
                        or (item.name.split(".")[0] if isinstance(node, ast.Import) else item.name),
                        str(getattr(node, "module", None)) + ":" + item.name,
                    )
                    for item in node.names
                ]
            for name, value in names:
                if name in ("scene", "subjects", "result"):
                    raise ValueError("Worker definitions use reserved scene name " + name)
                if name in bindings and bindings[name] != value:
                    raise ValueError("Worker definitions conflict for name " + name)
                bindings[name] = value
        sources.append(code.rstrip())
    fps = 24
    width, height = [int(value) for value in job["aspect"].split(":")]
    scale = 640 / max(width, height)
    resolution = [max(2, round(value * scale / 2) * 2) for value in (width, height)]
    setup = (
        "import bpy\n"
        "scene = bpy.context.scene\n"
        'bpy.ops.object.select_all(action="SELECT")\n'
        "bpy.ops.object.delete(use_global=False)\n"
        "scene.frame_start = 1\n"
        f'scene.frame_end = {round(job["duration"] * fps)}\n'
        f"scene.render.fps = {fps}\n"
        "scene.render.fps_base = 1.0\n"
        f"scene.render.resolution_x = {resolution[0]}\n"
        f"scene.render.resolution_y = {resolution[1]}\n"
        "scene.render.resolution_percentage = 100\n"
    )
    execution = (
        "subjects = build_subjects(scene)\n"
        "previs_restore_character_limbs(subjects)\n"
        "build_camera(scene, subjects)\n"
        "scene.frame_set(scene.frame_start)\n"
        'result = {"subjectCount": len(subjects)}\n'
    )
    code = setup + "\n\n".join(sources) + "\n\n" + execution
    compile(code, "<composed-previs>", "exec")
    return code


async def run_previs_workers(request, job, messages, emit, model):
    contract = {
        "units": "meters",
        "up_axis": "Z",
        "stage_front": "+Y",
        "fps": 24,
        "duration": job["duration"],
        "aspect": job["aspect"],
        "subjects_interface": "build_subjects(scene) returns a list of all actual subject root objects",
        "camera_interface": "build_camera(scene, subjects) returns and assigns scene.camera",
        "execution_order": "build_subjects(scene), then build_camera(scene, subjects), then commit; render separately",
        "ownership": "subject code owns subjects/environment; camera code owns camera only; both use the shared timeline",
        "default_motion_detail": "basic_motion",
        "camera_scale": "Use previs_camera_from_scale: relative spatial curves scale to actual full-motion subject envelope; normalized times control duration independently",
    }
    scenario_count = len(job.get("scenarioTitles") or []) or None
    if scenario_count:
        contract["scenario_scene_count"] = scenario_count
        contract["scenario_scene_timing"] = (
            "Produce exactly one timeline interval per numbered scenario scene, in order. "
            "Give each scene enough time to show its defining action near the midpoint. "
            "A scene boundary is not a cut if the user requested a continuous shot."
        )
    director_messages = list(messages)
    for attempt in range(3):
        emit("log", "메인 워커: 프롬프트를 정리하고 공통 타임라인을 구성합니다.")
        response = await request(
            "responses",
            {
                "model": model,
                "instructions": (
                    "You are the main previs director. First preprocess the free-form user request into "
                    "preprocessed_prompt with five concise bullet lists in the user language. "
                    "SCENE covers duration, cast count, location and central action. Use the contract duration and aspect. "
                    "CHOREOGRAPHY preserves the complete user choreography as text for final video generation. "
                    "Set motion_detail=basic_motion by default, with complete character anatomy, connected arm gestures, alternating steps and weight shifts appropriate to the action. "
                    "Rebuild missing limbs when the baseline contains head/body-only characters. "
                    "Preserve placement, formation, travel, facing and event timing. Keep full choreography as text for final video generation. "
                    "Use requested_detail for explicitly requested articulated actions, and blocking for explicitly requested static or omitted motion. "
                    "Use an empty choreography list when there is no choreographed action. "
                    "MOTION covers physical continuity, weight transfer, joint posture, timing and movement constraints. "
                    "CAMERA covers opening framing, movement by phase, subject coverage and final composition. "
                    "MOOD / STYLE covers atmosphere, visual style and performance tone. "
                    "Preserve explicit details and alternatives, including directions, counts and order. "
                    "Use the baseline for revisions, changing only the requested aspects. "
                    "When adding useful direction to an unspecified detail, frame it as a suggestion and keep it consistent with the request. "
                    "Then use these five sections to delegate subject/object action and camera design as separate tasks "
                    "that share one synchronized timeline. Return scene_action, camera "
                    "and chronological timeline intervals covering the entire requested duration. For each interval explain "
                    "what the subjects do and how camera framing, movement, gaze and speed should reveal that action. "
                    "For dynamic movement requests, make camera intensity explicit in the camera brief: intended viewpoint "
                    "angle change, lateral displacement relative to group width, forward distance, and framing targets. "
                    "Specify the intended visual change at opening, first movement, peak perspective change and ending for dynamic requests. "
                    "Use the actual baseline camera reports when available and set scene-specific projected width and height, "
                    "distance or angle targets by beat. Do not turn an inherited lens choice into a user requirement. "
                    "State the planned motion-envelope center and largest dimension as the reference scale, not the stage size. "
                    "Judge perceived perspective change, not merely total path length. A distant wide shot with a small "
                    "sideways sway is not dynamic. Choose scene-specific targets yourself and respect all user constraints. "
                    "Respect explicit constraints. For revisions preserve unrequested baseline behavior; for new scenes "
                    "design original action rather than a fixed preset. Use the request identifier only for unspecified creative "
                    "choices: "
                    + str(job.get("id", ""))
                    + ". Contract: "
                    + json.dumps(contract)
                    + ". "
                    + CAMERA_REVISION_POLICY
                    + "\n"
                    + NATURAL_PREVIS_POLICY
                    + "\n"
                    + CAMERA_FOUR_ELEMENT_POLICY
                ),
                "input": director_messages,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "previs_director_plan",
                        "strict": True,
                        "schema": PLAN_SCHEMA,
                    }
                },
            },
            stage_label="메인 워커 기획",
        )
        try:
            plan = read_json(response)
            validate_plan(plan, job["duration"], scenario_count)
            plan["prompt_markdown"] = format_preprocessed_prompt(plan.get("preprocessed_prompt"))
            break
        except (ValueError, TypeError, KeyError) as error:
            director_messages.append(
                {
                    "role": "user",
                    "content": "The director plan failed validation: "
                    + str(error)
                    + ". Correct the prompt sections and shared timeline and return valid JSON.",
                }
            )
            if attempt == 2:
                raise RuntimeError(
                    "메인 워커의 프롬프트·타임라인 검증이 3회 실패했습니다."
                ) from None
    plan["shared_contract"] = contract
    job["plan"] = plan
    job["workerDrafts"] = {}
    emit("checkpoint", None)
    context = {
        "request": job.get("request", ""),
        "apiContract": json.loads(previs_api_contract()),
        "plan": plan,
        "baseline": job.get("baseSnapshot"),
        "workflowInstructions": job.get("workflowInstructions", {}),
    }

    async def generate(role):
        label = "대상·동작 워커" if role == "subjects" else "카메라 워커"
        entry = "build_subjects(scene)" if role == "subjects" else "build_camera(scene, subjects)"
        role_rule = (
            "Construct complete anatomy-appropriate character proxies following plan.motion_detail. "
            "Humans need connected arms, hands, legs and feet. Rebuild these when a prior version contains only a head/body capsule. "
            "Animate basic gestures, grounded alternating steps and weight shifts along the shared staging and event timeline. "
            "For moving objects define contact partners and held/release/flight/impact/catch/settle phases as relevant. "
            "Use the common trajectory, ballistic, bounce, blending and rolling samplers documented below instead of disconnected phase formulas. "
            "Match position and velocity at non-impact boundaries, preserve rigid scale and coordinate character contact with object motion. "
            "Use previs_footplant_sample throughout grounded travel, previs_support_height for support reach, and previs_jump_sample for free flight when relevant. "
            "Design torso/pelvis anticipation and landing absorption plus role-specific reactions. "
            "Key character IK first; sample actual held-object contacts with previs_grip_position in a second pass. "
            "Implement specifically requested articulated detail when requested_detail is selected; respect explicit static or omitted motion. "
            "Return actual subject roots. Do not create or animate a camera."
            if role == "subjects"
            else "Create only the camera. Use the actual subjects argument and their evaluated geometry at timeline frames to "
            "choose composition, positions, gaze and speed. Match camera changes to subject action events. Do not alter "
            "subject geometry, animation, scene duration or choreography. Implement the director intensity and framing "
            "targets using evaluated scene measurements. Finish camera creation and keyframing before quality gating. "
            'Record unmet quality targets with previs_quality_check(condition,message,check="camera",key="stable-id"); '
            "do not abort the initial build for an aesthetic/framing/zoom target. Quality is reviewed separately after commit. "
            "Adapt your path and lens within explicit constraints; keep structural object/data errors distinct. "
            "Return and assign scene.camera. " + CAMERA_REVISION_POLICY
        )
        prompt = (
            "You are the "
            + label
            + ". Return Python definitions only, with entry "
            + entry
            + ". "
            + role_rule
            + " Shared interfaces: "
            + json.dumps(contract)
            + ". "
            "The other worker runs independently. Do not assume its object names or helper functions; use only the shared interface. "
            "Prefix additional helper functions _subject_ or _camera_ according to role. Imports and literal constants are allowed, "
            "but no top-level execution. Prefix module constants _subject_ or _camera_ by role to avoid shared-name collisions. "
            "The server combines both worker definitions verbatim and configures frame range, fps and aspect before calling the entries. "
            "Blender bpy and mathutils are available in the remote renderer. Use Vector for vector "
            "operations. Obtain the dependency graph with bpy.context.evaluated_depsgraph_get(), never scene.evaluated_depsgraph_get(). "
            "previs_camera_register_subjects(camera, subjects) accepts only camera and subjects. "
            "Pass required_markers only to previs_register_motion(root, markers, required_markers=...), never camera helpers. "
            "previs_humanoid(name, position, color) returns a rig dict containing root and parts. "
            "_pv_two_bone(start,end,first,second,bend_direction) returns tuple joint positions; convert to Vector. "
            "_pv_key(object,frame) writes transform keyframes. previs_register_motion(root,markers) registers actual joints. "
            "previs_camera_smooth(camera, positions=[(0,(x,y,z)),...,(1,(x,y,z))], "
            "targets=[(0,(x,y,z)),...,(1,(x,y,z))], subjects=subjects, focal_length=...) uses normalized times. "
            "Camera worker must construct the camera with previs_camera_from_scale(camera, subjects, positions, targets, "
            "reference_span, reference_center=(x,y,z), scale_axes=(True,True,True), fit_frame=True, "
            "frame_width=.8, frame_height=.94, focal_length=...). reference_span is the largest dimension of your "
            "planned subject motion envelope, and reference_center its center. Do not substitute stage size. "
            "It samples all actual animation frames, scales position/target curves, fits distance and logs scale/travel/speed. "
            "Choose frame_width from requested framing, not a mandatory .8. Locked axes retain world coordinates: "
            "fixed height means scale_axes=(True,True,False); absolute camera means all false and fit_frame=False. "
            "Disable fit_frame for explicit distance, exact trajectory or framing that intentionally crops subjects. "
            "Do not multiply spatial movement by duration; use the normalized timeline for speed. "
            "Do not use predefined subject motion or combined scene/camera presets. Do not allocate artifacts or render. "
            "Use scene.frame_start/end and restore scene.frame_current after sampling. For full-body framing register subjects "
            "with previs_camera_register_subjects or previs_camera_smooth(subjects=subjects). A revision must preserve "
            "the baseline part owned by your role unless the current user request changes it. "
            + "PREVIS_API_CONTRACT\n" + previs_api_contract() + "\n"
            + JSON_LOG_POLICY
            + "\n"
            + NATURAL_PREVIS_POLICY
            + "\n"
            + CAMERA_FOUR_ELEMENT_POLICY
        )
        worker_messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        record = {"status": "running", "attempts": []}
        job["workerDrafts"][role] = record
        emit("checkpoint", None)
        code = None
        issues = []
        for attempt in range(WORKER_DRAFT_ATTEMPTS):
            emit("log", f"{label}: 코드 생성·수정 {attempt+1}/{WORKER_DRAFT_ATTEMPTS}")
            patched = (
                await repair_previs_functions(
                    request, code, issues, model, stage_label=label + " 함수 보정"
                )
                if attempt and code
                else None
            )
            if patched is not None:
                code = patched
                draft = {"code": code, "notes": "Affected functions repaired"}
                issues = validate_worker(code, role)
            else:
                response = await request(
                    "responses",
                    {
                        "model": model,
                        "instructions": prompt,
                        "input": worker_messages,
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": (
                                    "previs_subject_worker"
                                    if role == "subjects"
                                    else "previs_camera_worker"
                                ),
                                "strict": True,
                                "schema": CODE_SCHEMA,
                            }
                        },
                    },
                    stage_label=label,
                )
                try:
                    draft = read_json(response)
                    if not isinstance(draft, dict):
                        raise ValueError("Invalid worker output")
                    code = draft.get("code")
                    issues = validate_worker(code, role)
                except (ValueError, TypeError) as error:
                    code = None
                    issues = [{"code": "WORKER_OUTPUT", "line": 1, "message": str(error)}]
                    draft = {}
            record["attempts"].append({"code": code, "issues": issues})
            emit("checkpoint", None)
            if not issues:
                record.update(status="passed", code=code, notes=draft.get("notes", ""))
                emit("checkpoint", None)
                emit("log", label + ": 코드 검증 통과")
                return
            worker_messages.extend(
                [
                    {"role": "assistant", "content": json.dumps(draft, ensure_ascii=False)},
                    {
                        "role": "user",
                        "content": (
                            "Validation found problems. Fix your code; if local fixes are insufficient, reconstruct the problematic part and fill missing implementation yourself. Keep user intent and the shared timeline. Never disable checks or substitute a preset. Issues: "
                            + json.dumps(issues, ensure_ascii=False)
                        ),
                    },
                ]
            )
        record.update(
            status="needs_main_repair",
            code=code,
            notes="Worker repairs are exhausted; main AI must repair/reconstruct the invalid or missing parts using the shared plan and these validation issues.",
        )
        emit("checkpoint", None)
        emit("log", label + ": 남은 검증 문제를 메인 AI에 인계해 보완합니다.")

    tasks = [asyncio.create_task(generate(role)) for role in ("subjects", "camera")]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for record in job["workerDrafts"].values():
            if record["status"] == "running":
                record["status"] = "cancelled"
        emit("checkpoint", None)
        raise
    emit("log", "서버가 두 워커의 코드를 조합하고 원격 씬을 구성합니다.")
    return plan
