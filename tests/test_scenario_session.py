from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
from ai_video.video_higgsfield_jutsu.python_backend.scenario_session import ScenarioSessions


def test_scenario_session_combines_three_field_previs_request(tmp_path):
    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    app = FastAPI()
    app.include_router(router)
    service = router.service
    service.execute = AsyncMock()
    client = TestClient(app)
    scenes = [{"title": title, "prompt": prompt} for title, prompt in (
        ("시작", "고양이가 앉아 있다."), ("발견", "나비가 날아온다."), ("추격", "고양이가 달린다.")
    )]
    with patch("ai_video.video_higgsfield_jutsu.python_backend.shortform_routes.generate_scenario",
               new=AsyncMock(return_value={"scenes": scenes})):
        response = client.post("/api/shortform/scenario", json={
            "title": "고양이", "summary": "나비를 쫓는다.", "model": service.default_model,
            "db_name": "tenant", "chatbotid": "bot",
        })
    assert response.status_code == 200
    session_id = response.json()["sessionId"]
    assert ScenarioSessions(service.scenarios.directory).get(session_id)["settings"]["db_name"] == "tenant"
    scenes[0] = {"title": "수정한 시작", "prompt": "고양이가 창가에 앉아 있다."}
    assert client.put(f"/api/shortform/scenario/{session_id}", json={"scenes": scenes}).status_code == 200
    with patch("ai_video.video_higgsfield_jutsu.python_backend.app.filter_user_prompts",
               new=AsyncMock(return_value={})):
        response = client.post("/api/previs/generate", headers={"sessionId": session_id},
                               json={"aspect": "9:16", "duration": "5", "message": "카메라 고정"})
    assert response.status_code == 202, response.text
    assert response.json()["sessionId"] == session_id
    job = service.store.get(response.json()["jobId"])["data"]
    assert "고양이가 창가에 앉아 있다." in job["request"]
    assert "추가 요청사항:\n카메라 고정" in job["request"]
    assert job["workSettings"]["db_name"] == "tenant"
    assert job["workSettings"]["chatbotid"] == "bot"


def test_previs_accepts_body_session_and_natural_language_requirements(tmp_path):
    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    app = FastAPI()
    app.include_router(router)
    service = router.service
    service.execute = AsyncMock()
    client = TestClient(app)
    for requirements in ("카메라 고정", "추가 요청사항:\n카메라 고정"):
        session_id = service.scenarios.save({
            "title": "응원", "summary": "응원한다.",
            "scenes": [{"title": "인사", "prompt": "손을 흔든다."}],
            "settings": {"model": service.default_model, "db_name": "", "chatbotid": ""},
        })
        with patch("ai_video.video_higgsfield_jutsu.python_backend.app.filter_user_prompts",
                   new=AsyncMock(return_value={})):
            response = client.post("/api/previs/generate", json={
                "sessionId": session_id, "message": requirements, "aspect": "16:9", "duration": "5",
                "db_name": "", "chatbotid": "", "model": service.default_model,
            })
        assert response.status_code == 202, response.text
        job = service.store.get(response.json()["jobId"])["data"]
        assert job["request"].endswith("추가 요청사항:\n카메라 고정")
        assert job["request"].count("추가 요청사항:") == 1
        assert "손을 흔든다." in job["request"]


@pytest.mark.parametrize("requirements", [{}, {"message": ""}, {"message": "   "}])
@pytest.mark.parametrize("session_location", ["header", "body", "both"])
def test_saved_scenario_needs_no_additional_requirements(tmp_path, requirements, session_location):
    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    app = FastAPI()
    app.include_router(router)
    service = router.service
    service.execute = AsyncMock()
    session_id = service.scenarios.save({
        "title": "인사", "summary": "창가에서 인사한다.",
        "scenes": [{"title": "시작", "prompt": "성인이 손을 흔든다."}],
        "settings": {"model": service.default_model, "db_name": "", "chatbotid": ""},
    })
    client = TestClient(app)
    with patch(
        "ai_video.video_higgsfield_jutsu.python_backend.app.filter_user_prompts",
        new=AsyncMock(return_value={}),
    ) as inspect:
        response = client.post(
            "/api/previs/generate",
            headers={"sessionId": session_id} if session_location in ("header", "both") else {},
            json={**requirements, **({"sessionId": session_id} if session_location in ("body", "both") else {})},
        )
    assert response.status_code == 202, response.text
    prompt = service.store.get(response.json()["jobId"])["data"]["request"]
    assert "성인이 손을 흔든다." in prompt
    assert "추가 요청사항:" not in prompt
    assert inspect.await_count == 1


def test_no_scenario_still_requires_scene_text(tmp_path):
    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post("/api/previs/generate", json={}).status_code == 400
    assert client.post("/api/previs/generate", json={"message": ""}).status_code == 400


@pytest.mark.parametrize("saved", [False, True])
def test_invalid_previs_reports_context_and_lengths_without_prompt(tmp_path, caplog, saved):
    import logging
    from uuid import uuid4
    from ai_video.video_higgsfield_jutsu.python_backend.scenario_session import scenario_prompt

    router = create_api_router(root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace())
    service = router.service
    record = {
        "title": "private title", "summary": "private summary",
        "scenes": [{"title": "private scene", "prompt": "x" * 8000}],
        "settings": {"model": service.default_model},
    }
    session_id = service.scenarios.save(record) if saved else str(uuid4())
    app = FastAPI()
    app.include_router(router)
    with caplog.at_level(logging.WARNING):
        response = TestClient(app).post("/api/previs/generate", json={
            "sessionId": session_id, "message": "", "aspect": "9:16", "duration": "15",
        })
    assert response.status_code == 400
    if saved:
        assert "8,000자" in response.json()["detail"]
        assert f"{len(scenario_prompt(record)):,}" in response.json()["detail"]
    else:
        assert "시나리오" in response.json()["detail"]
    assert "header_session=False body_session=True" in caplog.text
    assert f"scenario_loaded={saved}" in caplog.text
    assert "input_chars=0" in caplog.text
    assert "private title" not in caplog.text
    assert "private summary" not in caplog.text
    assert "x" * 80 not in caplog.text
