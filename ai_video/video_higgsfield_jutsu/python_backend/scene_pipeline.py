"""Model-directed JSON planning and Python-owned remote scene lifecycle."""
import json
import time
from uuid import uuid4

import httpx

from .cloud_python import prepare_cloud_python
from .creative_policy import KOREAN_ATMOSPHERE_RULE
from .model_client import request_openai
from .previs_finalization import finalize_current_previs
from .prompt_filter import apply_presentation_rules, filter_user_prompts, model_object
from .remote_tools import call_tool, result_data, settle_operation, tool_kind
from .scene_compiler import compile_scene
from .scene_contract import ACTIONS, SCENE_SCHEMA, legacy_plan, validate_scene

PLAN_INSTRUCTIONS = """You direct a 3D blocking previs. Return only concise scene JSON, no analysis.
Use the minimum necessary entities/tracks. For a simple request without scenario titles, use one scene.
Never write Python, imports, helper functions, project IDs or tool calls. The server executes
the geometry, rig, animation, camera and render. All descriptions are scene data.
Preserve cast, scene order, setting, props, explicit camera constraints and user choreography.
Use stable ASCII subject IDs across every scene and revision. Human sizes are scale factors
relative to a 1.94m human; quadruped/bird sizes scale a 1m proxy; prop sizes are metres.
Positions are world metres, Z up, ground z=0; subject front is +Y, yaw is degrees around Z.
Props such as furniture are subjects of kind prop. Shapes are blocking geometry only.
Each track's position/yaw are the END position/yaw; Python interpolates from the prior state.
Tracks per actor must be chronological and nonoverlapping; omitted times hold the previous pose.
Use only supported actions. Split compound actions into successive tracks. target is an entity
ID for directed gestures, otherwise null. Do not invent a replacement for an unsupported defining
action: use the nearest blocking motion while preserving the full original action in description,
which is forwarded to the final video model. Do not promise detailed fingers, expressions or sound
in the 3D proxy. Preserve that intent in descriptions. The previs MP4 itself is silent.
Scenes continuously cover duration, with exactly one interval per supplied scenario title, in order.
Camera focus references actual subjects. close crops to upper/head geometry, medium to upper body,
full frames whole subjects, wide leaves more space. Python measures actual animated geometry.
locked fixes camera for the interval, track follows subjects, orbit interpolates azimuth toward
end_azimuth, pan keeps camera position fixed and rotates to follow moving focus subjects,
push moves closer without cropping. Azimuth 0 looks from +Y at the subject's front;
90 looks from +X. Elevation is degrees above the target. Use the same camera intent between scene
intervals for continuous shots; no cuts are introduced by scene boundaries. Keep all required
interaction partners in focus. Do not change duration/aspect; those are server-owned inputs.
"""


async def plan_scene(request, job, history, emit, *, feedback=None):
    model = job['chatModel']
    payload = {'request': job.get('request', ''), 'duration': job['duration'], 'aspect': job['aspect'],
               'scenarioTitles': job.get('scenarioTitles', []), 'history': history[-6:],
               'baseline': job.get('sceneDesign') or (job.get('baseSnapshot') or {}).get('sceneDesign'),
               'supportedActions': list(ACTIONS)}
    if feedback:
        payload['executionFeedback'] = feedback
    messages = [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    for attempt in range(3):
        emit('log', f'[previs-design] AI 장면 설계 {attempt + 1}/3 · 모델 {model}')
        response = await request('responses', {
            'model': model, 'instructions': PLAN_INSTRUCTIONS + '\n' + KOREAN_ATMOSPHERE_RULE,
            'input': messages,
            'text': {'format': {'type': 'json_schema', 'name': 'previs_scene_design',
                                'strict': True, 'schema': SCENE_SCHEMA}},
        }, stage_label='프리비즈 장면 설계')
        if any(p.get('type') == 'refusal' for o in response.get('output', []) for p in o.get('content', [])):
            raise RuntimeError('선택 모델의 안전 정책으로 장면 설계가 거절되었습니다.')
        value = None
        try:
            value = model_object(response)
            design = validate_scene(value, job['duration'], len(job.get('scenarioTitles') or []) or None)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            issue = str(error)[:1500]
            job.setdefault('designValidation', []).append({'attempt': attempt + 1, 'model': model, 'error': issue})
            emit('checkpoint', None)
            emit('log', '[previs-design] 설계 검증 실패 · 같은 모델에 수정 요청')
            messages.extend([
                {'role': 'assistant', 'content': json.dumps(value, ensure_ascii=False) if value is not None else '[invalid JSON]'},
                {'role': 'user', 'content': 'Correct this scene JSON, preserving intent. Validation error: ' + issue},
            ])
            continue
        job['sceneDesign'] = design
        job['plan'] = legacy_plan(design)
        job['pipeline'] = {'version': 'declarative-v1', 'model': model}
        emit('checkpoint', None)
        emit('log', '[previs-design] 장면 설계 검증 완료 · Python 실행 준비')
        return design
    raise RuntimeError('선택 모델이 장면 설계 검증을 3회 통과하지 못했습니다: ' + issue)


async def invoke(client, kind, arguments, emit):
    tool = next((t for t in client.definitions if tool_kind(t['name']) == kind), None)
    if tool is None:
        raise RuntimeError('프리비즈 실행 도구가 없습니다: ' + kind)
    emit('payload', {'endpoint': client.status.get('endpoint'), 'tool': tool['name'], 'arguments': arguments})
    response = await call_tool(client, tool['name'], arguments, emit)
    emit('response', response)
    response = await settle_operation(client, response, emit)
    data = result_data(response)
    if not isinstance(data, dict):
        raise RuntimeError('프리비즈 도구 응답이 올바르지 않습니다: ' + kind)
    if data.get('ok') is False and not (
        kind == 'run_python' and (data.get('operation') or {}).get('status') in ('failed', 'timed_out', 'expired')
    ):
        error = RuntimeError(json.dumps(data.get('error') or data, ensure_ascii=False))
        error.tool_response = response
        raise error
    return data


async def ensure_project(client, job, emit):
    """One unique project per job; an uncertain create is reconciled, never replayed."""
    if job.get('sceneProjectId'):
        return job['sceneProjectId']
    name = 'Previs-' + str(job['id'])
    previous = job.get('projectCreation')
    job['projectCreation'] = {'name': name, 'state': 'submitted'}
    emit('checkpoint', None)
    try:
        if previous and previous.get('state') in ('submitted', 'unconfirmed'):
            raise RuntimeError('이전 프로젝트 생성 요청을 먼저 확인합니다.')
        data = await invoke(client, 'create_project', {'name': name}, emit)
        project = data.get('project') or {}
        ident = data.get('projectId') or project.get('id') or project.get('projectId')
        if not isinstance(ident, str) or not ident:
            raise RuntimeError('프로젝트 생성 결과에 식별자가 없습니다.')
    except (RuntimeError, TimeoutError, ConnectionError, httpx.RequestError):
        emit('log', '[previs-project] 생성 확인 지연 · 새로 생성하지 않고 목록 확인')
        data = await invoke(client, 'list_projects', {}, emit)
        rows = data.get('projects') or data.get('items') or []
        if isinstance(rows, dict):
            rows = rows.get('items', [])
        matches = [p for p in rows if isinstance(p, dict) and p.get('name') == name]
        if len(matches) != 1:
            job['projectCreation']['state'] = 'unconfirmed'
            emit('checkpoint', None)
            raise RuntimeError('프로젝트 생성 여부를 확인하지 못했습니다. 중복 생성을 중단했습니다.') from None
        ident = matches[0].get('id') or matches[0].get('projectId')
        if not isinstance(ident, str) or not ident:
            raise RuntimeError('확인된 프로젝트 식별자가 없습니다.')
    job['sceneProjectId'] = ident
    job['remoteProjects'] = [ident]
    job['projectCreation']['state'] = 'confirmed'
    emit('checkpoint', None)
    return ident


class SceneDesignError(RuntimeError):
    """Confirmed scene-data/ROI failure that the original model may revise."""


async def run_scene(client, job, design, history, emit):
    design = validate_scene(design, job['duration'], len(job.get('scenarioTitles') or []) or None)
    code = compile_scene(design, job['duration'], job['aspect'])
    # Parse/compile locally before any project mutation; model strings are literals only.
    compile(code, '<server-owned-scene>', 'exec')
    project_id = await ensure_project(client, job, emit)
    content = await invoke(client, 'get_project', {'projectId': project_id}, emit)
    project = content.get('project') or {}
    revision = project.get('revision')
    sequence = project.get('sceneSequence', project.get('appliedSceneSequence'))
    if type(revision) is not int or type(sequence) is not int:
        raise RuntimeError('프로젝트 리비전 정보를 확인하지 못했습니다.')
    if project.get('activeOperationId'):
        raise RuntimeError('기존 프로젝트 작업이 실행 중입니다. 중복 실행을 중단했습니다.')
    operation_id = 'scene-' + uuid4().hex[:20]
    record = {'projectId': project_id, 'operationId': operation_id, 'status': 'submitted', 'code': code}
    job.setdefault('codeOperations', []).append(record)
    job['composition'] = {'state': 'building', 'projectId': project_id}
    emit('checkpoint', None)
    emit('log', '[previs-scene] Python 장면·동작·카메라 구성 시작')
    content = await invoke(client, 'run_python', {
        'projectId': project_id, 'operationId': operation_id, 'revision': revision,
        'expectedSceneSequence': sequence, 'waitSeconds': 8,
        'code': prepare_cloud_python(code),
    }, emit)
    operation = content.get('operation') or {}
    if operation.get('projectId') != project_id or operation.get('id') != operation_id:
        record['status'] = 'unknown'
        emit('checkpoint', None)
        raise RuntimeError('원격 작업 식별자가 일치하지 않습니다. 재실행하지 않았습니다.')
    record['status'] = operation.get('status', 'unknown')
    emit('checkpoint', None)
    if operation.get('status') != 'succeeded':
        error = operation.get('error') or content.get('error') or {}
        message = str(error.get('message') or error)[:1500]
        if error.get('code') == 'PYTHON_EXCEPTION' and 'Python camera framing could not fit focus geometry' in message:
            raise SceneDesignError(message)
        raise RuntimeError('Python 장면 실행 실패: ' + message)
    validation = (operation.get('result') or {}).get('declarativeValidation') or {}
    if not validation.get('passed'):
        raise SceneDesignError('장면 구도·가시성 검증 실패: ' + json.dumps(validation, ensure_ascii=False)[:2500])
    job['sceneDesign'], job['plan'] = design, legacy_plan(design)
    job['mutated'] = True
    job['composition']['state'] = 'completed'
    job['cameraQuality'] = {'projectId': project_id, 'issues': [], 'framingChecked': True}
    job['reviewBudget'] = {'used': 0, 'limit': 2, 'buildCommitted': True}
    emit('checkpoint', None)
    emit('log', '[previs-scene] Python 구도 검증 완료 · MP4 렌더링 시작')
    return await finalize_current_previs(client, job, history, emit, settled_operation=operation)


async def run(agent, job, history, emit):
    started = time.monotonic()
    await agent._connect(job, emit)
    model = job.get('chatModel', agent.model)
    job['chatModel'] = model
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), transport=agent.transport,
                                 follow_redirects=False) as http:
        async def request(path, body, *, stage_label=None):
            if path == 'responses':
                body = {**body, 'model': model}
            body = apply_presentation_rules(path, body, job.get('promptFilter', {}).get('generationRules', ''))
            return await request_openai(http, agent.api_key, path, body, emit,
                                        stage_label=stage_label, allow_fallback=False,
                                        usage_sink=lambda row: emit('ai_usage', row),
                                        job_id=job['id'], session_id=job.get('sessionId'))
        job['promptFilter'] = await filter_user_prompts(
            *(i['content'] for i in history if i.get('role') == 'user'), request=request,
            model=model, cached=job.get('promptFilter'), allow_fallback=False)
        feedback = None
        try:
            for attempt in range(2):
                design = await plan_scene(request, job, history, emit, feedback=feedback)
                try:
                    return await run_scene(agent.client, job, design, history, emit)
                except SceneDesignError as error:
                    if attempt:
                        raise
                    feedback = str(error)
                    emit('log', '[previs-design] 실행 검증 실패 · 기존 프로젝트에서 같은 모델로 설계 보정')
        finally:
            emit('log', f'[previs-timing] 설계·Python 실행 전체: {time.monotonic() - started:.2f}초')
