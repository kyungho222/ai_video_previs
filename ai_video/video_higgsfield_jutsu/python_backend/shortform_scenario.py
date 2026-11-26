"""Create one editable shortform scenario using the configured chat model."""

import json
import logging
import re

import httpx

from .model_client import request_openai
from .ai_usage import report_usage
from .pipeline_log import logger
from .prompt_filter import apply_presentation_rules, check_local_terms, filter_user_prompts, model_object


SCENARIO_INSTRUCTIONS = (
    'Write one Korean video scenario: 3 scenes, 4 only if needed, chronological JSON. '
    'Each has a short title and compact prompt: actor/action, camera, required sound. '
    'Use 1-2 sentences excluding dialogue. Preserve all requested actors, events, order, dialogue '
    'verbatim and explicit direction; never shorten dialogue for brevity. Shared cast/setting only '
    'in scene 1; later scenes describe changes. Begin the requested action immediately, without '
    'invented preparation. No-cut requests use continuous action phases. Default to natural Korean '
    'settings; traditional details only when appropriate. Honor specified cultures/styles/references. '
    'Keep EVERY scene frontal unless explicitly directed otherwise, including close/wide shots and '
    'push/pull movements. Looking at an object does not override this. Do not invent side/rear, '
    'overhead, orbit or reverse views. Omit numbering, markdown, total duration and aspect ratio.'
)


SCENARIO_FORMAT = {
    "type": "json_schema", "name": "shortform_scenario", "strict": True,
    "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"title": {"type": "string"}, "prompt": {"type": "string"}},
                    "required": ["title", "prompt"],
                },
            },
        },
        "required": ["scenes"],
    },
}


OUTPUT_SETTING = re.compile(
    r"화면\s*비율\s*[:：]?\s*\d{1,2}\s*[:：]\s*\d{1,2}\b"
    r"|(?<=[,·;])\s*(?:16\s*[:：]\s*9|9\s*[:：]\s*16|1\s*[:：]\s*1|4\s*[:：]\s*3|3\s*[:：]\s*4)(?=\s*[,·;.!?]|\s*$)"
    r"|(?:영상\s*)?전체\s*(?:길이\s*)?[:：]?\s*\d+(?:\.\d+)?\s*(?:초|seconds?\b)"
    r"|(?<=[,·;])\s*\d+(?:\.\d+)?\s*(?:초|seconds?\b)(?=\s*[,·;.!?]|\s*$)",
    re.IGNORECASE,
)


def clean_scene_output(text):
    text = OUTPUT_SETTING.sub("", " ".join(text.split()))
    text = re.sub(r"(?:\s*[,·;])+\s*(?=[.!?]|$)", "", text)
    return text.strip()


async def generate_scenario(*, title, summary, model, api_key, transport, request_id=None, usage_sink=None,
                            admission_cache=None):
    usage = []
    def capture(row):
        usage.append(row)
        report_usage(usage_sink, row)
    logger.info("[요청 %s] 시나리오 프롬프트 검사 시작", request_id)
    cached_review = admission_cache.get(title, summary, model) if admission_cache is not None else None
    review = await filter_user_prompts(
        title, summary, model=model, api_key=api_key, transport=transport, usage_sink=capture,
        allow_fallback=True, combined=True,
        cached=cached_review,
    )
    if admission_cache is not None and review is not cached_review:
        admission_cache.put(title, summary, model, review)
    if cached_review is not None and review is cached_review:
        logger.info("[요청 %s] 동일 입력의 사전검사 재사용 · 유효기간 연장 없음", request_id)
    body = {
        "model": model,
        "instructions": SCENARIO_INSTRUCTIONS,
        "input": [
            {"role": "user", "content": json.dumps({"title": title, "summary": summary}, ensure_ascii=False)},
        ],
        "text": {"format": SCENARIO_FORMAT},
    }
    body = apply_presentation_rules("responses", body, review["generationRules"])
    logger.info("[요청 %s] 시나리오 AI 생성 시작 · 모델 %s", request_id, model)
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), transport=transport,
                                 follow_redirects=False) as http:
        response = await request_openai(
            http, api_key, "responses", body,
            lambda _kind, value: logging.getLogger(__name__).info("%s", value),
            stage_label="숏폼 시나리오 생성", usage_sink=capture, allow_fallback=True,
        )
    try:
        scenes = model_object(response)["scenes"]
        if not isinstance(scenes, list) or len(scenes) not in (3, 4):
            raise ValueError("Invalid scene count")
        normalized = []
        for scene in scenes:
            title = clean_scene_output(scene["title"])
            prompt = clean_scene_output(scene["prompt"])
            if not 1 <= len(title) <= 120 or not 1 <= len(prompt) <= 4000:
                raise ValueError("Invalid scene length")
            normalized.append({"title": title, "prompt": prompt})
    except (AttributeError, KeyError, TypeError, ValueError):
        raise RuntimeError("AI가 완성된 씬 3~4개를 반환하지 않았습니다. 다시 시도하세요.") from None
    check_local_terms("\n".join(scene["title"] + "\n" + scene["prompt"] for scene in normalized))
    return {"scenes": normalized, "ai_usage": usage}
