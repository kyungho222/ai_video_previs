"""Adapt common API response envelopes into a scene object without model-name rules."""
import json


def extract_scene_object(response):
    if not isinstance(response, dict):
        raise ValueError('모델 응답은 JSON 객체여야 합니다.')
    if response.get('status') not in (None, 'completed'):
        raise ValueError('모델 응답이 완료되지 않았습니다.')
    if isinstance(response.get('structuredContent'), dict):
        return response['structuredContent']
    chunks = []
    for item in response.get('output', []):
        if isinstance(item, dict):
            chunks += [part.get('text', '') for part in item.get('content', [])
                       if isinstance(part, dict) and part.get('type') == 'output_text']
    if not chunks:
        choices = response.get('choices')
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            content = (choices[0].get('message') or {}).get('content')
            if isinstance(content, str):
                chunks = [content]
    if not chunks and isinstance(response.get('content'), list):
        chunks = [part.get('text', '') for part in response['content']
                  if isinstance(part, dict) and part.get('type') == 'text']
    if not chunks and isinstance(response.get('candidates'), list) and response['candidates']:
        content = response['candidates'][0].get('content') or {}
        chunks = [part.get('text', '') for part in content.get('parts', []) if isinstance(part, dict)]
    try:
        value = json.loads(''.join(chunks))
    except (ValueError, TypeError) as error:
        raise ValueError('모델 장면 응답에서 JSON 객체를 읽을 수 없습니다.') from error
    if not isinstance(value, dict):
        raise ValueError('모델 장면 응답은 JSON 객체여야 합니다.')
    return value
