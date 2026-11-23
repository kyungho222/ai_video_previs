"""Resolve server-owned model aliases and send with the matching credential.

Only model identifiers come from callers. Endpoints and keys come from the
packaged mapping or server environment. Providers never borrow OpenAI keys.
"""
from dataclasses import dataclass, field
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from dotenv import dotenv_values
import httpx

from .protocol import chat_payload, response_payload


BASES = {
    'openai': 'https://api.openai.com/v1',
    'deepseek': 'https://api.deepseek.com/v1',
    'xai': 'https://api.x.ai/v1',
    'mistral': 'https://api.mistral.ai/v1',
    'qwen': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
    'alibaba': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
    'moonshot': 'https://api.moonshot.ai/v1',
    'kimi': 'https://api.moonshot.ai/v1',
    'google': 'https://generativelanguage.googleapis.com/v1beta/openai',
    'anthropic': 'https://api.anthropic.com/v1',
}
KEYS = {
    'openai': ('OPENAI_API_KEY',), 'deepseek': ('DEEPSEEK_API_KEY',),
    'xai': ('XAI_API_KEY',), 'mistral': ('MISTRAL_API_KEY',),
    'qwen': ('ALIBABA_API_KEY','DASHSCOPE_API_KEY','QWEN_API_KEY'),
    'alibaba': ('ALIBABA_API_KEY','DASHSCOPE_API_KEY'),
    'moonshot': ('MOONSHOT_API_KEY','KIMI_API_KEY'),
    'kimi': ('MOONSHOT_API_KEY','KIMI_API_KEY'),
    'google': ('GOOGLE_API_KEY','GEMINI_API_KEY'),
    'anthropic': ('ANTHROPIC_API_KEY',), 'local': ('LOCAL_API_KEY',),
}
FALLBACK_MODEL = 'gpt-6.1-terra'


@dataclass(frozen=True)
class ModelRoute:
    requested: str
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    options: dict = field(default_factory=dict, repr=False)


@lru_cache(maxsize=1)
def _catalog():
    path = Path(__file__).with_name('model_mapping.json')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    return value.get('model_mappings', {}), value.get('disabled_model_mappings', {})


def available_models():
    mappings = _catalog()[0]
    return list(dict.fromkeys([*mappings, *(item['api_model'] for item in mappings.values())]))


def canonical_model(requested):
    """Resolve aliases; missing, disabled or unknown identifiers use the safe default."""
    if not isinstance(requested, str):
        return _fallback_model(requested, 'missing' if requested is None else 'invalid_type')
    requested = requested.strip()
    mappings, disabled = _catalog()
    if requested in disabled:
        return _fallback_model(requested, 'disabled')
    if requested in mappings:
        return mappings[requested]['api_model']
    if any(item.get('api_model') == requested for item in mappings.values()):
        return requested
    return _fallback_model(requested, 'unmatched' if requested else 'missing')


def _fallback_model(requested, reason):
    # Model fields are untrusted: do not echo URLs, credentials, multiline data or objects.
    safe_requested = (requested if isinstance(requested, str)
                      and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}', requested)
                      else '<' + type(requested).__name__ + '>')
    logging.getLogger('uvicorn.error').warning(
        '[model-routing] fallback · 모델 매칭 실패 · requested=%s · selected=%s · reason=%s',
        safe_requested, FALLBACK_MODEL, reason,
    )
    return FALLBACK_MODEL


def model_options(config, *, default=FALLBACK_MODEL):
    """Return unique API model identifiers and public configuration status only."""
    options = {}
    for alias, mapping in _catalog()[0].items():
        value = mapping['api_model']
        if value in options:
            continue
        try:
            resolve_model(alias, config=config)
            configured = True
        except ValueError:
            configured = False
        label = alias if alias == value else alias + ' · ' + value
        options[value] = {'value':value, 'label':label, 'provider':mapping['provider'], 'configured':configured}
    first = canonical_model(default)
    return sorted(options.values(), key=lambda item: (item['value'] != first, item['provider'], item['value']))


def environment_config():
    root = Path(__file__).resolve().parents[2]
    return {**dotenv_values(root / '.env'),
            **dotenv_values(root / 'ai_video/video_higgsfield_jutsu/.env'), **os.environ}


def resolve_model(requested, *, config=None, openai_key=None):
    original = requested
    requested = canonical_model(requested)
    mappings, _ = _catalog()
    mapping = next((item for item in mappings.values() if item.get('api_model') == requested), {})
    provider = mapping.get('provider', 'openai')
    config = environment_config() if config is None else config
    key_provider = mapping.get('api_key_name') or provider
    names = KEYS.get(key_provider)
    if names is None:
        raise ValueError('지원하지 않는 AI 공급사입니다: ' + provider)
    key = next((config.get(name) for name in names if config.get(name)), None)
    if not key and key_provider == 'openai':
        key = openai_key
    if not key and key_provider == 'local':
        key = 'LOCAL_EMPTY'
    if not key:
        raise ValueError('선택한 AI 모델의 API 키를 .env에 설정하세요: ' + ' / '.join(names))
    base = (config.get(provider.upper() + '_API_BASE_URL')
            or (config.get('OPENAI_BASE_URL') if provider == 'openai' else None)
            or mapping.get('api_base') or BASES.get(provider))
    parsed = urlsplit(base or '')
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('AI API 주소는 인증정보와 쿼리가 없는 HTTPS 주소여야 합니다.')
    return ModelRoute(original if isinstance(original,str) else '', provider, requested, base.rstrip('/'), str(key), mapping)


async def _post_once(http, openai_key, path, body, *, config=None, route=None):
    # Moderation is a separate OpenAI safety service, not a chat-model alias.
    if path != 'responses':
        if path != 'moderations':
            raise ValueError('지원하지 않는 AI 요청 경로입니다.')
        if not openai_key:
            raise ValueError('콘텐츠 안전 검사를 위한 OPENAI_API_KEY가 필요합니다.')
        return await http.post('https://api.openai.com/v1/' + path,
                               headers={'Authorization':'Bearer ' + openai_key}, json=body)
    route = route or resolve_model(body.get('model'), config=config, openai_key=openai_key)
    headers = {'Authorization':'Bearer ' + route.api_key}
    settings = environment_config() if config is None else config
    if route.provider == 'anthropic' and settings.get('ANTHROPIC_WORKSPACE_ID'):
        headers['anthropic-workspace-id'] = settings['ANTHROPIC_WORKSPACE_ID']
    native = route.provider == 'openai'
    payload = {**body,'model':route.model} if native else chat_payload(body,route)
    response = await http.post(route.base_url + ('/responses' if native else '/chat/completions'),
                               headers=headers, json=payload)
    if response.is_success and not native:
        try:
            value = response_payload(response.json())
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            response = httpx.Response(502, request=response.request,
                                      json={'error':{'code':'INVALID_MODEL_RESPONSE'}})
        else:
            clean_headers = {key:value for key,value in response.headers.items()
                             if key.lower() not in ('content-length','content-encoding')}
            response = httpx.Response(response.status_code, headers=clean_headers,
                                      request=response.request, json=value)
    response.extensions['model_route'] = {'provider':route.provider,'model':route.model}
    return response


SAFETY_ERRORS = {'content_policy_violation', 'content_filter', 'safety_violation', 'moderation_blocked'}


def _safe_code(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',value) else 'unknown_error'


def _response_failure(response, body):
    try:
        return _response_failure_data(response, body)
    except (TypeError, AttributeError, KeyError, IndexError):
        return 'invalid_model_response', False


def _response_failure_data(response, body):
    try:
        data = response.json()
    except ValueError:
        return 'invalid_json_response', False
    if not isinstance(data,dict):
        return 'invalid_json_response', False
    error = data.get('error') or {}
    if not isinstance(error,dict):
        error = {}
    code = _safe_code(error.get('code') or error.get('type'))
    if not response.is_success or error:
        return code, code in SAFETY_ERRORS
    for item in data.get('output') or []:
        if any(part.get('type') == 'refusal' for part in item.get('content') or []):
            return None, True
    if data.get('status') in ('failed','incomplete','cancelled'):
        reason = (data.get('incomplete_details') or {}).get('reason')
        return 'incomplete_response', reason in SAFETY_ERRORS
    fmt = body.get('text',{}).get('format',{})
    if fmt.get('type') == 'json_schema' and not any(item.get('type') == 'function_call' for item in data.get('output') or []):
        text = data.get('output_text') or ''.join(
            part.get('text','') for item in data.get('output') or []
            for part in item.get('content') or [] if part.get('type') == 'output_text')
        if text:
            try:
                json.loads(text)
            except (ValueError,TypeError):
                return 'invalid_structured_response', False
    return None, False


def _log_failure(body, route, status, code, *, fallback):
    stage = _safe_code(body.get('text',{}).get('format',{}).get('name') or 'responses')
    logging.getLogger('uvicorn.error').warning(
        '[model-routing] %s · 호출 실패 · stage=%s · provider=%s · model=%s · HTTP=%s · code=%s · selected=%s',
        'fallback' if fallback else 'call_failed', stage,
        route.provider if route else 'unresolved', route.model if route else canonical_model(body.get('model')),
        status, code, FALLBACK_MODEL if fallback else 'none',
    )


async def routed_post(http, openai_key, path, body, *, config=None, allow_fallback=True):
    """Try the selected chat model, then GPT once on an operational failure."""
    if path != 'responses':
        response = await _post_once(http, openai_key, path, body, config=config)
        if not response.is_success:
            code, _ = _response_failure(response, body)
            logging.getLogger('uvicorn.error').warning(
                '[model-routing] call_failed · stage=%s · provider=openai · model=%s · HTTP=%s · code=%s · fallback=not_applicable',
                path, _safe_code(body.get('model')), response.status_code, code,
            )
        return response
    route = None
    response = None
    error = None
    code = None
    safety = False
    try:
        route = resolve_model(body.get('model'), config=config, openai_key=openai_key)
        response = await _post_once(http, openai_key, path, body, config=config, route=route)
        code, safety = _response_failure(response, body)
        if code is None:
            return response
    except (httpx.TransportError, ValueError) as caught:
        error = caught
        code = type(caught).__name__
    selected_model = route.model if route else canonical_model(body.get('model'))
    fallback = allow_fallback and selected_model != FALLBACK_MODEL and not safety
    _log_failure(body, route, response.status_code if response else '-', code, fallback=fallback)
    if not fallback:
        if error:
            raise error
        return response
    fallback_body = {**body, 'model':FALLBACK_MODEL}
    fallback_route = resolve_model(FALLBACK_MODEL, config=config, openai_key=openai_key)
    try:
        result = await _post_once(http, openai_key, path, fallback_body, config=config, route=fallback_route)
    except httpx.TransportError as error:
        # Preserve the selected fallback even when no HTTP response is available.
        error.model_fallback = {'provider':'openai', 'model':FALLBACK_MODEL}
        _log_failure(fallback_body, fallback_route, '-', type(error).__name__, fallback=False)
        raise
    result.extensions['model_fallback'] = {'from':selected_model, 'to':FALLBACK_MODEL, 'reason':code}
    fallback_code, _ = _response_failure(result, fallback_body)
    if fallback_code:
        _log_failure(fallback_body, fallback_route, result.status_code, fallback_code, fallback=False)
    return result
