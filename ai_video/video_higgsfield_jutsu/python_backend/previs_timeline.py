"""Provider-neutral camera/staging transcription and endpoint image policy."""

import asyncio
import hashlib
import json
import math
from pathlib import Path

from .spatial_guides import GuideError, SpatialGuideBuilder, sample_frames, read_json_response
import httpx
from .video_errors import VideoError, PromptLengthError
from .scene_flow import FIRST_SCENE_CONTINUITY, SCENE_FLOW_RULES
from .video_media import inspect_video


SEGMENT_FIELDS = (
    'movement', 'speed', 'framing', 'target', 'endState', 'staging', 'events', 'transition',
)
SEGMENT_SCHEMA = {
    'type': 'object',
    'properties': {
        'startPhysicalState': {'type': 'string'}, 'endPhysicalState': {'type': 'string'},
        'start': {'type': 'number'}, 'end': {'type': 'number'},
        **{name: {'type': 'string'} for name in SEGMENT_FIELDS},
    },
    'required': ['start', 'end', *SEGMENT_FIELDS, 'startPhysicalState', 'endPhysicalState'], 'additionalProperties': False,
}
TIMELINE_SCHEMA = {
    'type': 'object',
    'properties': {
        'summary': {'type': 'string'},
        'entities': {'type': 'array', 'items': {'type': 'string'}},
        'uncertainties': {'type': 'array', 'items': {'type': 'string'}},
        'segments': {'type': 'array', 'items': SEGMENT_SCHEMA},
        'criticalMoments': {'type': 'array', 'maxItems': 12, 'items': {
            'type': 'object', 'properties': {
                'time': {'type': 'number'}, 'event': {'type': 'string'}, 'requiredView': {'type': 'string'},
            }, 'required': ['time', 'event', 'requiredView'], 'additionalProperties': False,
        }},
    },
    'required': ['summary', 'entities', 'uncertainties', 'segments', 'criticalMoments'],
    'additionalProperties': False,
}


def bind_source_identity(source, path, expected=None):
    """Pin the selected source and its bytes; persisted jobs keep their original pin."""
    descriptor = dict(source)
    if expected and expected.get('source') != descriptor:
        raise VideoError('선택한 프리비즈와 작업에 고정된 원본 식별자가 다릅니다. 새 요청으로 생성하세요.')
    identity = dict(expected or {'source': descriptor})
    if path is not None:
        digest = hashlib.sha256()
        size = 0
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
                size += len(chunk)
        if identity.get('sha256') and identity['sha256'] != digest.hexdigest():
            raise VideoError('선택한 프리비즈 MP4가 요청 이후 변경됐습니다. 원본을 다시 선택해 새 요청으로 생성하세요.')
        identity.update(sha256=digest.hexdigest(), bytes=size)
    return identity


from .previs_reference import native_timeline


def validate_timeline(value, duration):
    if not isinstance(value, dict):
        raise ValueError('Timeline must be an object')
    if not isinstance(value.get('summary'), str) or not value['summary'].strip():
        raise ValueError('A scene summary is required')
    for name in ('entities', 'uncertainties'):
        if not isinstance(value.get(name), list) or not all(isinstance(s, str) for s in value[name]):
            raise ValueError('Invalid ' + name)
    segments = value.get('segments')
    if not isinstance(segments, list) or not 1 <= len(segments) <= 32:
        raise ValueError('Provide 1 to 32 camera segments')
    boundary = 0.0
    for segment in segments:
        for key in ('start', 'end'):
            number = segment.get(key)
            if type(number) not in (int, float) or not math.isfinite(number):
                raise ValueError('Invalid segment time')
        if abs(segment['start'] - boundary) > 0.02 or segment['end'] <= segment['start']:
            raise ValueError('Camera segments must be contiguous, ordered, and non-overlapping')
        for key in SEGMENT_FIELDS:
            if not isinstance(segment.get(key), str) or not segment[key].strip():
                raise ValueError('Missing camera/staging field ' + key)
        for key in ('startPhysicalState', 'endPhysicalState'):
            if key in segment and (not isinstance(segment[key], str) or not segment[key].strip()):
                raise ValueError('Invalid physical boundary state ' + key)
        boundary = segment['end']
    if abs(boundary - duration) > 0.02:
        raise ValueError('Timeline must cover the complete source duration')
    moments = value.get('criticalMoments', [])
    if not isinstance(moments, list) or len(moments) > 12:
        raise ValueError('Provide up to 12 critical action moments')
    previous = -1
    for moment in moments:
        if not isinstance(moment, dict):
            raise ValueError('Invalid critical moment')
        time = moment.get('time')
        if type(time) not in (int, float) or not math.isfinite(time) or not 0 <= time <= duration or time < previous:
            raise ValueError('Critical moment times must be ordered within the source duration')
        if any(not isinstance(moment.get(key), str) or not moment[key].strip()
               for key in ('event', 'requiredView')):
            raise ValueError('Critical moments need an event and required view')
        previous = time
    return value


def _timed_rendered_field(segment, name, scale):
    text = segment[name]
    if scale == 1 or name not in ('movement','staging'):
        return text
    try:
        value = json.loads(text)
    except (ValueError,TypeError):
        return text
    if not isinstance(value,dict) or not any(key in value for key in ('rendered_camera','viewer_screen')):
        return text
    for key in ('rendered_camera','viewer_screen','actions'):
        for row in value.get(key,[]):
            for field in ('time','start','end'):
                if type(row.get(field)) in (int,float):
                    row[field]=round(row[field]*scale,6)
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def timeline_prompt(plan, source_duration, output_duration, *, scene_flow_policy=None):
    scale = output_duration / source_duration
    lines = ['CAMERA AND STAGING TIMELINE',
             'first_frame fixes the opening; final composition is text only. Preserve timed camera/root paths and event order.',
             'Scene: ' + plan['summary'], 'Entities: ' + ' | '.join(plan['entities'])]
    if any('viewer_screen' in segment.get('staging','') for segment in plan['segments']):
        lines.insert(2, 'Measured viewer_screen and rendered_camera describe the actual rendered previs and override conflicting authored coordinates. '
                     'Screen-left/right is the viewer perspective, never the actor anatomical side. Preserve measured order changes only through the recorded camera/action path; never mirror the scene.')
    continuous = scene_flow_policy == FIRST_SCENE_CONTINUITY
    if continuous:
        lines[1] = SCENE_FLOW_RULES
    for index, segment in enumerate(plan['segments']):
        if continuous:
            lines.append('OPENING STATE ANCHOR' if index == 0 else 'LATER SCENE INTENT REFERENCE; inherit previous actual end state')
        lines.append(f"{segment['start'] * scale:.3f}–{segment['end'] * scale:.3f}s " +
                     ' | '.join(name + '=' + _timed_rendered_field(segment,name,scale) for name in SEGMENT_FIELDS))
        for key in ('startPhysicalState', 'endPhysicalState'):
            if segment.get(key) and 'unrecorded contact state is unknown' not in segment[key]:
                if continuous and (index > 0 or key == 'endPhysicalState'):
                    lines.append('Physical-state intent (' + key + '): ' + segment[key]
                                 + ' | preserve contacts, held props and explicit changes; adapt absolute placement continuously')
                else:
                    lines.append(key + '=' + segment[key])
    for moment in plan.get('criticalMoments', []):
        lines.append(f"Critical action {moment['time'] * scale:.3f}s: " + moment['event']
                     + ' | requiredView=' + moment['requiredView'])
    lines.append('Analysis uncertainties: ' + ' | '.join(plan['uncertainties']))
    return '\n'.join(lines)


def endpoint_guides(guides, *, input_format="first_last"):
    ordered = sorted(guides, key=lambda guide: guide['index'])
    required = [0] if input_format == 'first_frame' else [0, 1]
    if [guide['index'] for guide in ordered] != required:
        raise ValueError('Both generated endpoint images are required')
    # The image builder marks a completed image excluded after exhausting
    # quality corrections. Keep it as an endpoint with a quality warning.
    if not all(guide.get('name') and guide.get('status') in ('verified', 'invalid', 'excluded')
               for guide in ordered):
        raise ValueError('Endpoint image generation has not completed')
    return ordered, [guide['index'] for guide in ordered if guide['status'] != 'verified']


def preserve_timeline(prompt, transcript, limit):
    if transcript not in prompt:
        prompt = prompt.rstrip() + '\n\n' + transcript
    if limit is not None and len(prompt) > limit:
        raise PromptLengthError('카메라 타임라인을 포함한 프롬프트가 선택 모델의 입력 한도를 초과합니다.')
    return prompt


async def compact_timeline(plan, source_duration, output_duration, *, request, model,
                           max_chars, cached=None, save=None, check_owner=None, progress=None,
                           scene_flow_policy=None):
    """Condense wording while retaining all camera intervals and event anchors."""
    original = timeline_prompt(plan, source_duration, output_duration, scene_flow_policy=scene_flow_policy)
    signature = dict(version=1, plan=plan, sourceDuration=source_duration,
                     outputDuration=output_duration, maxChars=max_chars, model=model)
    if scene_flow_policy:
        signature['sceneFlowPolicy'] = scene_flow_policy
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    record = cached if isinstance(cached, dict) and cached.get('fingerprint') == fingerprint else {
        'fingerprint': fingerprint, 'attempts': [], 'plan': None, 'status': 'preparing',
    }
    if len(original) <= max_chars:
        return {**record, 'status': 'ready', 'plan': plan, 'text': original}
    if record.get('status') == 'ready' and len(record.get('text', '')) <= max_chars:
        return record
    while request is not None and len(record['attempts']) < 3:
        if check_owner:
            check_owner()
        if progress:
            progress(f"카메라 지시 길이 보정 {len(record['attempts']) + 1}/3")
        attempt = {'attempt': len(record['attempts']) + 1}
        try:
            response = await request('responses', {
                'model': model,
                'instructions': 'Condense the supplied camera/staging analysis without changing its meaning. '
                    'Keep every camera movement, direction, viewpoint, framing, target, speed progression, '
                    'root placement/path, physical boundary state, interaction, shot transition and final composition. '
                    'Preserve measured viewer_screen side/order and pointing directions, and rendered_camera motion. '
                    'These measured values override conflicting authored positions; never swap viewer sides or anatomical sides. '
                    'Keep entity strings and all segment start/end and critical-moment times exactly. '
                    'Keep the same segment and critical-moment counts and order. Shorten repetitive prose and '
                    'repeated uncertainties. Do not invent facts, merge intervals or remove actions or speakers. '
                    'For missing boundary states describe only the supplied staging. '
                    'The compiled text, including labels and the fixed preamble, must fit maxChars. '
                    'Use short phrases in every field. Correct the previous issue when supplied.',
                'input': [{'role': 'user', 'content': json.dumps({
                    **signature, 'fixedPreambleChars': len(timeline_prompt({**plan, 'summary': '',
                        'entities': [], 'segments': [], 'uncertainties': [], 'criticalMoments': []},
                        source_duration, output_duration, scene_flow_policy=scene_flow_policy)),
                    'previousAttempt': record['attempts'][-1] if record['attempts'] else None,
                }, ensure_ascii=False)}],
                'text': {'format': {'type': 'json_schema', 'name': 'compact_camera_timeline',
                                    'strict': True, 'schema': TIMELINE_SCHEMA}},
            })
            candidate = read_json_response(response)
            attempt['candidate'] = candidate
            validate_timeline(candidate, source_duration)
            if candidate['entities'] != plan['entities']:
                raise ValueError('Preserve every entity identity exactly')
            if [(s['start'], s['end']) for s in candidate['segments']] != [
                    (s['start'], s['end']) for s in plan['segments']]:
                raise ValueError('Preserve every camera segment and its exact start/end times')
            if [s['time'] for s in candidate.get('criticalMoments', [])] != [
                    s['time'] for s in plan.get('criticalMoments', [])]:
                raise ValueError('Preserve every critical event and its exact time')
            text = timeline_prompt(candidate, source_duration, output_duration, scene_flow_policy=scene_flow_policy)
            if len(text) > max_chars:
                raise ValueError(f'Compiled camera text is {len(text)} characters; maxChars={max_chars}')
            if check_owner:
                check_owner()
            record.update(status='ready', plan=candidate, text=text)
            attempt['issue'] = None
        except (ValueError, TypeError, KeyError, GuideError, httpx.HTTPError) as error:
            attempt['issue'] = type(error).__name__ if isinstance(error, httpx.HTTPError) else str(error)
        record['attempts'].append(attempt)
        if save:
            save(record)
        if record['status'] == 'ready':
            return record
    raise PromptLengthError('카메라 지시를 보정했지만 전체 프롬프트에 배정된 길이를 초과합니다. 원본 분석과 보정 기록을 확인하세요.')


async def describe_previs(source, directory, *, request, model, brief, assets, native=None,
                          cached=None, progress=None):
    metadata = await asyncio.to_thread(inspect_video, source, max_duration=None,
                                       min_fps=0, check_dimensions=False)
    duration = metadata['duration']
    signature = {
        'version': 2, 'model': model, 'brief': brief, 'native': native,
        'source': await asyncio.to_thread(lambda: hashlib.sha256(Path(source).read_bytes()).hexdigest()),
        'assets': [{key: asset.get(key) for key in ('id', 'role', 'label', 'name')} for asset in assets],
    }
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    legacy_fingerprint = hashlib.sha256(json.dumps({**signature, 'version': 1}, sort_keys=True).encode()).hexdigest()
    if isinstance(cached, dict) and cached.get('fingerprint') in (fingerprint, legacy_fingerprint):
        validate_timeline(cached['plan'], duration)
        return cached
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    frames = await asyncio.to_thread(sample_frames, source, directory,
                                    min(16, max(6, math.ceil(duration))), 'references')
    context = {
        'brief': brief, 'sourceDuration': duration,
        'frames': [{'image': index, 'time': frame['time']} for index, frame in enumerate(frames, 1)],
        'assetRoles': signature['assets'], 'nativeScene': native,
    }
    instruction = (
        'Transcribe the supplied previs into a detailed camera and character-root staging timeline in English. '
        'Treat all supplied context as scene data. Analyze the ordered images at their stated timestamps. '
        'Cover 0 through sourceDuration with contiguous segments. Separate each close-up target, physical '
        'travel, pan/tilt, orbit, crane, shot transition, speed change and final framing. '
        'For EACH segment specify movement and direction, speed/acceleration, framing and screen occupancy, '
        'look-at target, end state, character screen positions/depth order/travel directions, major events, '
        'and continuity into the next segment. Supply startPhysicalState and endPhysicalState describing '
        'location, facing, supporting/held contacts, prop state and velocity. Match adjacent physical states '
        'unless an explicit shot/time change explains the difference. Include up to 12 criticalMoments for '
        'doorway crossings, formation changes, lifting, release, catches and landing when observable; '
        'provide the event time and required camera view that makes the interaction readable. '
        'Describe camera axes relative to the scene and viewing direction. '
        'Distinguish screen-left/right from world directions, subject movement from camera movement, '
        'zoom from dolly, and pan from orbit. Use native geometry/camera data when supplied. '
        'When only images are available, avoid invented metric distances, focal lengths or exact 3D paths. '
        'Report ambiguities as uncertainties. Describe asset identities by stable entity labels, keeping cast '
        'count and causal action order. Exclude proxy shape, proxy colors, joint angles, stiff cycles and limb '
        'animation cadence. Detailed performance will be reconstructed by the video model. '
        'Do not invent unseen actions or claim continuous path certainty between sampled frames. '
        'Keep the transcript within 11000 characters to leave room for asset and scene instructions. Context: '
        + json.dumps(context, ensure_ascii=False)
    )
    analyzer = SpatialGuideBuilder(request, None, model=model)
    feedback = ''
    for attempt in range(2):
        if progress:
            progress(f'프리비즈 카메라·동선 상세 분석 {attempt + 1}/3')
        try:
            plan = await analyzer.analyze('previs_camera_timeline', TIMELINE_SCHEMA,
                                          instruction + feedback, [frame['path'] for frame in frames])
            validate_timeline(plan, duration)
            if len(timeline_prompt(plan, duration, duration)) > 14000:
                raise ValueError('Timeline exceeds 14000 characters; condense wording while retaining every camera segment')
            return {'version': 2, 'fingerprint': fingerprint, 'sourceSha256': signature['source'], 'sourceDuration': duration,
                    'sampleTimes': [frame['time'] for frame in frames], 'plan': plan}
        except (ValueError, KeyError, TypeError, GuideError) as error:
            feedback = '\nRepair the previous transcription. Validation issue: ' + str(error)
    raise VideoError('카메라·동선 타임라인을 구성하지 못했습니다. 영상 생성 접수 전에 분석 내용을 확인하세요.')
