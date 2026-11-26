"""Model-directed JSON planning and Python-owned remote scene lifecycle."""
import json
import time
from uuid import uuid4

import httpx

from .cloud_python import prepare_cloud_python
from .creative_policy import FRONT_CAMERA_RULE, KOREAN_ATMOSPHERE_RULE
from .model_client import request_openai
from .previs_finalization import finalize_current_previs
from .prompt_filter import apply_presentation_rules, filter_user_prompts
from .remote_tools import call_tool, result_data, settle_operation, tool_kind
from .scene_compiler import compile_scene
from .scene_correction import correct_scene
from .object_union import merge_requested_props, extract_prompt_hints
from .scene_relations import reconcile_relations, derive_movement_limits, prepare_initial_contacts, RelationError
from .scene_state import normalize_support_relations, resolve_scene_state
from .scene_contract import CAMERA_ELEVATION, legacy_plan, validate_scene
from .common_scene_adapter import adapt_scene_response, prepare_scene_execution
from .scene_adapter_completion import adapt_with_completion
from .canonical_scene import normalize_scene
from .scene_response_adapter import extract_scene_object
from .scene_direction import DIRECTION_SCHEMA, compile_direction, direction_candidate, direction_wire_schema

from .scene_intent import prepare_authored_direction, assess_intent, request_sources

# Exactly one authored design; execution warnings do not trigger AI redesign.
DESIGN_ATTEMPT_LIMIT = 1

PLAN_INSTRUCTIONS = f"""You direct a 3D blocking previs. Return only concise direction JSON, no analysis.
Write one ordered beats sequence, not independent action tracks and camera scenes.
Each beat owns its actions and camera together. Python derives both from that same beat.
Use the minimum necessary subjects and beats.
Never write Python, imports, helper functions, project IDs or tool calls. The server executes
the geometry, rig, animation, camera and render. All descriptions are scene data.
Preserve cast, scene order, setting, props, explicit camera constraints and user choreography. Python-extracted hints are literal keyword candidates, not verified facts: interpret them against the complete request and negations.
Unless the user explicitly requests a wait, stillness or a timed introduction, start the first
requested action at time 0. Do not add an opening idle interval for preparation, smiling,
standing or establishing the setting. Show introductory group/background framing while the
requested action is already happening. Allocate the full duration to all requested actions
and performers in their requested order; later performers may wait for their own turn.
When a baseline is provided, the latest request is a revision: interpret questions about
missing behavior as a request to correct it. Change the requested beats while
preserving unrelated baseline details, and prioritize this revision over earlier text.
Return relations separately from subjects for explicit or essential spatial relationships.
Use kind=supported_by for standing/performing ON a floor, stage or platform.
Being on a stage is a support relationship, not in_front_of. Root positions are feet/base
anchors; human height belongs to size, never root altitude. Grounded actions inherit their
support surface; only explicitly airborne actions may leave it.
Supported kinds: left_of/right_of and behind/in_front_of. Set referenceFrame=screen by default:
left/right refer to the viewer of the first reference shot; front/behind refer to camera depth.
Use referenceFrame=world only for explicit world axes, or object for explicit target-object orientation.
Use scope=entire only when a relation must persist throughout,
otherwise initial. evidence is the short user phrase supporting that relationship.
Do not infer a screen-relative direction as a world direction when the camera changes.
Preserve the initial camera reference as cameras change; do not reposition objects solely to preserve screen order in later shots.
Use kind=unsupported for required relations that cannot be represented faithfully; never silently omit them.
Use stable ASCII subject IDs across every scene and revision. Human sizes are scale factors
relative to a 1.94m human; quadruped/bird sizes scale a 1m proxy; prop sizes are metres.
Positions are world metres, Z up, ground z=0; subject front is +Y, yaw is degrees around Z.
Props such as furniture are subjects of kind prop. Shapes are blocking geometry only.
Each action's position/yaw are the END ROOT/FEET position/yaw, never a hand or held prop location.
Stationary reach/hold/point gestures keep the actor root on its established support; animate the prop separately.
Keep furniture footprints separate from standing actor roots and place cups on visible supporting surfaces.
Python interpolates root transforms from the prior state.
Every action lasts for its enclosing beat. Simultaneous posture or movement, arm gesture, and speech may share a beat. Use only one action per channel and do not combine posture change with movement in the same beat. Omitted actors hold their prior pose.
Each action has role=speaker for dialogue, role=performer for physical action, role=observer for listening or
background reactions. Prefer OMITTING idle observers entirely. Observers do not become camera
targets in scope=performers. Dialogue MUST use role=speaker and action=speak (small speaking gestures), not idle;
keep the actual words in intent. This silent proxy does not animate phoneme lip-sync.
purpose=performance is a requested action; purpose=introduction is an establishing beat;
purpose=wait is an explicitly requested pause. Untimed idle introductions are removed by Python.
Use only supported actions. Split successive actions into separate beats. target is an entity ID for directed gestures; point may target the reserved camera reference, otherwise null.
For both-hand waving use wave_both; for a hand-heart use hand_heart; for clapping use clap;
for a kick use kick, for a foot stomp use stomp, and for broad dance blocking use dance.
Use kick_left or stomp_left when the left leg is specified. Use salute, handshake, push,
pull and arms_up for those gestures; step_side, step_back and foot_tap use the left foot.
Use existing kick and stomp for the right foot. Represent unlisted detailed gestures with
the closest supported limb motion while retaining the original description.
These are approximate limb movements; preserve detailed hand/foot intent in descriptions.
Do not invent a replacement for an unsupported defining
action: use the nearest blocking motion while preserving the full original action in description,
which is forwarded to the final video model. Do not promise detailed fingers, expressions or sound
in the 3D proxy. Preserve that intent in descriptions. The previs MP4 itself is silent.
Beats cover the complete requested performance in order. card is a zero-based scenario-card index;
cover all supplied cards in order, with MULTIPLE beats per card when needed. Without cards use card=0.
For sequential greetings/individual close-ups, write one beat per speaker in the requested order.
For simultaneous group actions, put every participating member's action in the SAME beat.
weight is relative screen time (positive); seconds=null lets Python allocate the remaining duration
by weight. Set seconds to a number ONLY for timing explicitly requested by the user.
For seconds, copy the exact original duration phrase into timingEvidence.
An explicitly requested untimed wait also needs its verbatim wait phrase in timingEvidence; otherwise use null.
requirements lists concise explicit constraints with id, kind, sourceQuote copied verbatim from request,
and zero-based beats implementing it, value (framing/transition/action enum or null), subjects (stable IDs).
For individual close-ups list a separate camera requirement value=close per speaker. For a cut request
use value=cut. Setting/appearance/dialogue requirements use value=null and remain semantically unverified.
Include all named performers, sequential acts, required props,
framing and transition instructions. Use [] only when there are no explicit requirements.
Never combine successive camera framings or actor actions into one beat, even within one card. Do not write
start/end times; Python assigns contiguous intervals starting at 0 within the requested duration.
camera.scope=performers follows the actors/objects in that beat's actions and their target partners;
scope=all includes the whole cast. There is no independent focus list. An introductory group shot
uses scope=all while the first requested action is already happening. A group finale includes
all participating actions and scope=all. intent preserves dialogue and detailed performance intent;
do not put unimplemented camera changes or additional actions only in prose: encode them as beats.
Camera smoothing preserves movement continuity only; it must not hide required actors or actions.
Camera subjects are derived by Python from each beat. close crops to upper/head geometry, medium to upper body,
full frames whole subjects, wide leaves more space. Python measures actual animated geometry.
locked fixes camera for the interval, track follows subjects, orbit interpolates azimuth toward
end_azimuth, pan keeps camera position fixed and rotates to follow moving focus subjects,
push moves closer without cropping. Azimuth 0 looks from +Y at the subject's front;
90 looks from +X. With default yaw 0 and no explicit user camera direction, set
azimuth=end_azimuth=0 in every scene and do not use orbit. For rotated subjects,
align the camera with their actual front (azimuth=-yaw), not a fixed world side.
Only explicit user view-angle overrides receive camera.directionEvidence: copy the exact source phrase.
Use null otherwise; Python enforces frontal default and removes unrequested orbit.
Keep this frontal direction through tracking, close-ups and pull-backs.
Elevation is a finite number in degrees above the target, from
{CAMERA_ELEVATION['minimum']} to {CAMERA_ELEVATION['maximum']} inclusive. Use the same camera intent between scene
intervals for continuous shots. camera.transition controls entry into each shot: cut switches
immediately, continuous moves smoothly. Honor explicit cut/fixed-shot-switch requests with cut;
use continuous for explicit continuous movement or when unspecified. locked concerns only
movement WITHIN a shot; it does not prohibit a cut at the start of the shot. Keep all required
interaction partners in focus. Do not change duration/aspect; those are server-owned inputs.
"""


async def plan_scene(request, job, history, emit, *, feedback=None):
    model = job['chatModel']
    current_request = job.get('request', '')
    context = [item for item in history[-6:] if not (
        item.get('role') == 'user' and item.get('content', '').strip() == current_request.strip())]
    payload = {'request': job.get('request', ''), 'duration': job['duration'], 'aspect': job['aspect'],
               'scenarioTitles': job.get('scenarioTitles', []), 'history': context,
               'baseline': job.get('sceneDirection') or (job.get('baseSnapshot') or {}).get('sceneDirection')
                   or job.get('sceneDesign') or (job.get('baseSnapshot') or {}).get('sceneDesign')}
    hints = extract_prompt_hints(job.get('scenePrompt') or current_request)
    job['pythonPromptHints'] = hints
    payload['pythonExtractedHints'] = hints
    if feedback:
        payload['executionFeedback'] = {'validationIssues': feedback,
            'instruction': 'Correct ALL listed issues together in one complete scene JSON. Preserve user intent.'}
    messages = [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    budget = job.setdefault('designBudget', {'used': 0, 'limit': DESIGN_ATTEMPT_LIMIT})
    budget['limit'] = DESIGN_ATTEMPT_LIMIT
    while budget['used'] < DESIGN_ATTEMPT_LIMIT:
        budget['used'] += 1
        attempt = budget['used']
        emit('log', f'[previs-design] AI 장면 설계 {attempt}/{DESIGN_ATTEMPT_LIMIT} · 모델 {model}')
        response = await request('responses', {
            'model': model, 'instructions': PLAN_INSTRUCTIONS + '\n' + KOREAN_ATMOSPHERE_RULE + '\n' + FRONT_CAMERA_RULE,
            'input': messages,
            'text': {'format': {'type': 'json_schema', 'name': 'previs_scene_design',
                                'strict': True, 'schema': direction_wire_schema()}},
        }, stage_label='프리비즈 장면 설계')
        job['rawSceneModelResponse'] = response
        if any(p.get('type') == 'refusal' for o in response.get('output', []) for p in o.get('content', [])):
            raise RuntimeError('선택 모델의 안전 정책으로 장면 설계가 거절되었습니다.')
        value = None
        try:
            value = extract_scene_object(response)
            job['rawSceneDirection'] = value
            adapted = await adapt_with_completion(request, response, job, source=request_sources(current_request, history),
                                           duration=job['duration'], titles=job.get('scenarioTitles') or ())
            if job.get('sceneAdapterCompletion', {}).get('used'):
                completion = job['sceneAdapterCompletion']
                emit('log', f"[previs-adapter] 필수값 부분 보완 · {completion['status']} · 적용 {len(completion.get('applied', []))}개")
            value = adapted['direction']
            job['normalizedSceneDirection'] = adapted['normalized']
            job['normalizationChanges'] = adapted['changes']
            job['canonicalSceneStatus'] = 'normalized'
            job.setdefault('directionAdjustments', []).extend(adapted['adjustments'])
            job['lastSceneDesignCandidate'] = value
            job['lastSceneDirectionCandidate'] = value
            design = adapted['execution']
            job['sceneDirection'] = value
            job['sceneAdapter'] = {'version': adapted['version'], 'conventions': adapted['conventions'],
                                   'normalizationChanges': adapted['changes'], 'status': 'authored_ready'}

        except (ValueError, TypeError, KeyError, AttributeError) as error:
            trace = getattr(error, 'scene_adapter_trace', None)
            if trace:
                job['rawSceneDirection'] = trace['raw']
                job['normalizedSceneDirection'] = trace['normalized']
                job['normalizationChanges'] = trace['changes']
                job['sceneAdapter'] = {'version':trace['version'], 'status':'failed'}
            issues = getattr(error, 'issues', None) or [{'path': 'scene', 'message': str(error)}]
            issue = str(error)
            job['canonicalSceneStatus'] = 'failed'
            job.setdefault('designValidation', []).append({'attempt': attempt, 'model': model,
                                                          'error': issue, 'issues': issues})
            emit('checkpoint', None)
            emit('log', '[previs-design] 장면 정규화·실행 검증 실패 · 의미 손실 방지를 위해 중단')
            messages.extend([
                {'role': 'assistant', 'content': json.dumps(value, ensure_ascii=False) if value is not None else '[invalid JSON]'},
                {'role': 'user', 'content': json.dumps({
                    'instruction': 'Correct ALL listed issues together in one complete scene JSON. Preserve user intent. Do not fix only the first issue.',
                    'validationIssues': issues}, ensure_ascii=False)},
            ])
            continue
        job['sceneDesign'] = design
        job['plan'] = legacy_plan(design)
        job['pipeline'] = {'version': 'direction-v2', 'model': model}
        emit('checkpoint', None)
        emit('log', '[previs-design] 장면 설계 검증 완료 · Python 실행 준비')
        return design
    failures = job.get('designValidation') or []
    detail = failures[-1].get('error', '알 수 없는 형식 오류') if failures else '알 수 없는 형식 오류'
    emit('checkpoint', None)
    raise RuntimeError('장면 정규화·실행 검증 실패: ' + str(detail))


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

    def __init__(self, message, issues=None):
        self.issues = issues or [{'path': 'execution', 'message': message}]
        super().__init__(message)


def execution_validation_issues(validation):
    """Keep every failed runtime check and subject in one model feedback batch."""
    issues = []
    for check, result in validation.items():
        if not isinstance(result, dict) or result.get('passed') is not False:
            continue
        subjects = result.get('subjects') or []
        blocked = [s for s in subjects if isinstance(s, dict) and s.get('blockedBodyFrames', 0) > 0]
        if blocked:
            issues.extend({'path': check, 'message': 'Subject visibility failed', **s} for s in blocked)
        else:
            issues.append({'path': check, 'message': 'Runtime validation failed', 'details': result})
    return issues or [{'path': 'execution', 'message': 'Runtime validation failed', 'details': validation}]


async def run_scene(client, job, design, history, emit):
    scene_count = None if job.get('pipeline', {}).get('version') == 'direction-v2' else len(job.get('scenarioTitles') or []) or None
    design = validate_scene(design, job['duration'], scene_count)
    try:
        prepared = prepare_scene_execution(design,
            relations=(job.get('sceneDirection') or {}).get('relations') or [],
            request=job.get('scenePrompt') or job.get('request', ''))
    except RelationError as error:
        job['relationQuality'] = {'status': 'failed', 'error': str(error)}
        emit('checkpoint', None)
        raise RuntimeError('객체 관계 품질 검증 실패: ' + str(error)) from error
    design = prepared['execution']
    job['objectUnion'] = prepared['objects']
    job['sceneDesignBeforeCorrection'] = prepared['beforeCorrection']
    job['serverSceneCorrection'] = prepared['correction']
    job['relationQuality'] = prepared['relationQuality']
    job['sceneState'] = prepared['sceneState']
    job.setdefault('sceneAdapter', {}).update(version=prepared['version'],
        conventions=prepared['conventions'], unresolved=prepared['unresolved'], status='execution_ready')
    design = validate_scene(design, job['duration'], scene_count)
    emit('log', '[previs-adapter] 공통 장면 변환 완료 · 미확정 ' + str(len(prepared['unresolved'])) + '건')
    emit('checkpoint', None)
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
    job['runtimeValidation'] = validation
    job['serverSceneCorrection']['runtime'] = (operation.get('result') or {}).get('serverCorrection', {})
    projection_warnings = job['serverSceneCorrection']['runtime'].get('projectionWarnings') or []
    seen_projection_issues = set()
    for row in projection_warnings:
        if not isinstance(row, dict):
            continue
        fresh = [issue for issue in row.get('issues', []) if issue not in seen_projection_issues]
        if fresh:
            seen_projection_issues.update(fresh)
            emit('log', '[previs-camera] 구도 검사 측정 · ' + json.dumps(row, ensure_ascii=False))
    motion_warnings = job['serverSceneCorrection']['runtime'].get('cameraMotionWarnings', [])
    if motion_warnings:
        emit('log', f'[previs-correction] 화면 맞춤 추가 이동 {len(motion_warnings)}프레임 · 보정 기록 저장 · 추가 AI 호출 없음')
    if not validation.get('passed'):
        issues = execution_validation_issues(validation)
        job.setdefault('designExecutionValidation', []).append({'issues': issues, 'validation': validation})
        issue = '장면 구도·가시성 검증 실패: ' + json.dumps(issues, ensure_ascii=False)
        job.setdefault('designExecutionWarnings', []).append(issue)
        emit('log', '[previs-design] 검증 경고 · 보정 한도 도달 · 현재 최종 장면으로 렌더링 계속')
    job['sceneDesign'], job['plan'] = design, legacy_plan(design)
    job['mutated'] = True
    job['composition']['state'] = 'completed'
    runtime = job['serverSceneCorrection']['runtime']
    quality = assess_intent(job.get('sceneDirection'), design, runtime, request_sources(job.get('request', ''), history))
    if not validation.get('passed'):
        quality['status'] = 'mismatch'
        quality['issues'].extend(i for i in execution_validation_issues(validation)
                                 if i.get('path') != 'camera' or not runtime.get('cameraQuality'))
    quality['limitations'] = [r for r in job.get('directionAdjustments', [])
                              if r.get('reason') == 'front_start_only_for_turn']
    job['intentQuality'] = quality
    job['cameraQuality'] = {'projectId': project_id, 'issues': quality['issues'],
                            'status': 'mismatch' if quality['issues'] else quality['camera']['status'],
                            'framingChecked': bool(runtime.get('cameraQuality'))}
    emit('log', '[previs-intent] 실행 불일치 ' + str(len(quality['issues'])) + '건 · 의미 일치 확인 불가 · 추가 AI 호출 없음')
    if quality.get('status') == 'mismatch':
        job.setdefault('qualityWarnings', []).append({'stage': 'previs_execution', 'issues': quality.get('issues', [])})
        emit('log', '[previs-quality] 장면 실행 조건 미달 · 경고 기록 후 렌더링 계속')
        emit('checkpoint', None)
    job['reviewBudget'] = {'used': 0, 'limit': 2, 'buildCommitted': True}
    emit('checkpoint', None)
    emit('log', '[previs-scene] ' + ('Python 구도 검증 완료' if validation.get('passed') else 'Python 구도 검증 경고')
         + ' · MP4 렌더링 시작')
    return await finalize_current_previs(client, job, history, emit, settled_operation=operation)


async def build_scene(request, client, job, history, emit):
    job['runtimeCorrectionBudget'] = {'used': 0, 'limit': 0}
    design = await plan_scene(request, job, history, emit)
    return await run_scene(client, job, design, history, emit)


async def run(agent, job, history, emit):
    started = time.monotonic()
    await agent._connect(job, emit)
    model = job.get('chatModel', agent.model)
    job['chatModel'] = model
    job['designBudget'] = {'used': 0, 'limit': DESIGN_ATTEMPT_LIMIT}
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), transport=agent.transport,
                                 follow_redirects=False) as http:
        async def request(path, body, *, stage_label=None):
            if path == 'responses':
                body = {**body, 'model': model}
                if body.get('text', {}).get('format', {}).get('name') == 'previs_output_review':
                    stage_label = '프리비즈 요청·영상 일치 검증'
            body = apply_presentation_rules(path, body, job.get('promptFilter', {}).get('generationRules', ''))
            return await request_openai(http, agent.api_key, path, body, emit,
                                        stage_label=stage_label, allow_fallback=body.get('text',{}).get('format',{}).get('name') != 'scene_adapter_completion',
                                        usage_sink=lambda row: emit('ai_usage', row),
                                        job_id=job['id'], session_id=job.get('sessionId'))
        job['promptFilter'] = await filter_user_prompts(
            *(i['content'] for i in history if i.get('role') == 'user'), request=request,
            model=model, cached=job.get('promptFilter'), allow_fallback=True, combined=True)
        try:
            return await build_scene(request, agent.client, job, history, emit)
        finally:
            emit('log', f'[previs-timing] 설계·Python 실행 전체: {time.monotonic() - started:.2f}초')
