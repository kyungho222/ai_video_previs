import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
from test_prompt_filter import http_inspection_response


def test_shortform_generates_ordered_scenes_with_the_selected_chat_model(tmp_path, monkeypatch, caplog):
    # Preserve coverage for restoring frontend selection after the temporary override.
    monkeypatch.setattr('ai_video.video_higgsfield_jutsu.python_backend.work_settings.FORCE_DEFAULT_CHAT_MODEL', False)
    caplog.set_level("INFO", logger="uvicorn.error")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("PREVIS_CHAT_MODELS", "gpt-4.1-mini")
    calls = []
    scenario = {"scenes": [
        {"title": "발을 핥는 고양이", "prompt": "창가에서 고양이가 앞발을 핥는다."},
        {"title": "날아드는 나비", "prompt": "나비가 날아들고 고양이가 고개를 든다."},
        {"title": "나비를 쫓는 고양이", "prompt": "고양이가 나비를 쫓아 달린다. 고정 카메라, 컷 전환 없음, 전체 10초, 16:9."},
    ]}

    def respond(request):
        body = json.loads(request.content)
        if body.get("text", {}).get("format", {}).get("name") != "shortform_scenario":
            return http_inspection_response(request)
        calls.append(body)
        return httpx.Response(200, json={
            "status": "completed", "usage": {"input_tokens": 30, "output_tokens": 20, "total_tokens": 50},
            "output": [{"content": [{"type": "output_text", "text": json.dumps(scenario)}]}],
        })

    app = FastAPI()
    app.include_router(create_api_router(
        root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace(),
        transport=httpx.MockTransport(respond),
    ))
    client = TestClient(app)
    operation = client.get("/openapi.json").json()["paths"]["/api/shortform/scenario"]["post"]
    assert operation["tags"] == ["shortform"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert set(request_schema["properties"]) == {"title", "summary", "model", "db_name", "chatbotid"}
    assert not {"db_name", "chatbotid"}.intersection(request_schema["required"])
    forbidden = client.post("/api/shortform/scenario", headers={"Origin": "https://untrusted.invalid"}, json={})
    assert forbidden.status_code == 403
    response = client.post("/api/shortform/scenario", json={
        "title": "고양이", "summary": "고양이가 달린다. 10초, 16:9.",
        "model": "gpt-4.1-mini", "db_name": "example_db", "chatbotid": 42,
    })
    assert response.status_code == 200
    data = response.json()
    expected_scenes = scenario["scenes"][:2] + [{
        "title": "나비를 쫓는 고양이",
        "prompt": "고양이가 나비를 쫓아 달린다. 고정 카메라, 컷 전환 없음.",
    }]
    assert data["scenes"] == expected_scenes
    assert data["ai_usage"][-1]["total_tokens"] == 50
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-4.1-mini"
    assert json.loads(calls[0]["input"][-1]["content"]) == {
        "title": "고양이", "summary": "고양이가 달린다. 10초, 16:9.",
    }
    schema = calls[0]["text"]["format"]["schema"]
    assert schema["required"] == ["scenes"]
    assert schema["properties"]["scenes"]["items"]["required"] == ["title", "prompt"]

    blocked = client.post("/api/shortform/scenario", json={
        "title": "인형뽑기", "summary": "씨발", "model": "gpt-4.1-mini",
    })
    assert blocked.status_code == 422
    assert len(calls) == 1

    scenario["scenes"].pop()
    incomplete = client.post("/api/shortform/scenario", json={"title": "고양이", "summary": "고양이가 달린다."})
    assert incomplete.status_code == 502
    assert "시나리오 생성 요청 접수" in caplog.text
    assert "프롬프트 검사 시작" in caplog.text
    assert "시나리오 생성 완료 · 씬 3개" in caplog.text
    assert "요청 실패 · HTTP 422" in caplog.text
    assert "요청 실패 · HTTP 502" in caplog.text
    assert "고양이가 달린다." not in caplog.text
