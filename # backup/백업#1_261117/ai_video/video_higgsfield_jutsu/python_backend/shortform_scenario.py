"""Create one editable shortform scenario using the configured chat model."""

import json
import logging
import re

import httpx

from .model_client import request_openai
from .pipeline_log import logger
from .prompt_filter import apply_presentation_rules, check_local_terms, filter_user_prompts, model_object


SCENARIO_INSTRUCTIONS = """제목과 내용을 바탕으로 바로 영상 생성에 사용할 한국어 시나리오 하나를 작성하세요.
시나리오를 시간 순서의 3~4개 씬으로 나누세요. 단순한 요청은 보통 3개, 행동이나 전환이 더 필요하면 4개로 구성하세요.
각 씬의 title은 핵심 행동을 드러내는 짧은 제목입니다. prompt는 화면에 보이는 행동과 필요한 카메라·소리 지시를 담은 간결한 한 문단입니다. 번호, 항목명, 마크다운은 넣지 마세요.
각 씬은 준비 동작, 계기나 반응, 주요 행동 또는 결과처럼 구별되는 순간을 맡습니다. 앞 씬의 동작과 배경, 주체가 다음 씬으로 자연스럽게 이어져야 합니다.
예를 들어 '고양이가 달린다'는 고양이가 앞발을 핥는다 → 나비가 날아들어 고양이가 고개를 든다 → 고양이가 나비를 쫓아 달린다로 확장할 수 있습니다.
사용자가 명시한 행동과 연출 조건을 우선하세요. 컷 전환 없이 촬영하는 요청은 연속 촬영 안의 행동 구간으로만 나누세요.
영상 전체 길이, 화면 비율, 가로형·세로형 같은 출력 형식은 제목과 프롬프트에서 생략하세요. 씬의 행동과 순서만 묘사하세요."""

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


async def generate_scenario(*, title, summary, model, api_key, transport, request_id=None):
    usage = []
    logger.info("[요청 %s] 시나리오 프롬프트 검사 시작", request_id)
    review = await filter_user_prompts(
        title, summary, model=model, api_key=api_key, transport=transport, usage_sink=usage.append,
    )
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
            stage_label="숏폼 시나리오 생성", usage_sink=usage.append,
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
