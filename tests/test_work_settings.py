from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_video.video_higgsfield_jutsu.python_backend.app import create_api_router
from ai_video.video_higgsfield_jutsu.python_backend.video_models import VideoCapabilities
from ai_video.video_higgsfield_jutsu.python_backend.work_settings import video_analysis_model
from test_prompt_filter import http_inspection_response


def test_video_inherits_persisted_previs_work_settings(tmp_path, monkeypatch, caplog):
    # Verify the original selection behaviour remains available when the override is disabled.
    monkeypatch.setattr('ai_video.video_higgsfield_jutsu.python_backend.work_settings.FORCE_DEFAULT_CHAT_MODEL', False)
    caplog.set_level("INFO", logger="uvicorn.error")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("PREVIS_DEFAULT_CHAT_MODEL", "gpt-6.1-terra")
    monkeypatch.setenv("PREVIS_CHAT_MODELS", "gpt-6.1-terra,gpt-4.1")
    monkeypatch.setenv("VIDEO_PROMPT_MODEL", "gpt-6.1-terra")
    monkeypatch.setenv("VIDEO_REGION", "beijing")
    router = create_api_router(
        root=tmp_path, python_client=SimpleNamespace(), agent=SimpleNamespace(),
        transport=httpx.MockTransport(http_inspection_response),
    )
    service = router.service
    service.execute = AsyncMock()
    manager = router.video
    manager.adapters["fake"] = SimpleNamespace(configured=True, capabilities=VideoCapabilities(
        provider="fake", label="Fake", model="fake-video", regions=("singapore", "beijing"),
    ))
    manager.source = Mock(return_value={})
    manager.ensure = Mock()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    settings = {"model": "gpt-4.1", "db_name": "", "chatbotid": "00042"}

    submitted = client.post("/api/chat", json={"message": "A cat follows a butterfly.", **settings})
    assert submitted.status_code == 202, submitted.text
    previs_id = submitted.json()["jobId"]
    saved_previs = service.store.get(previs_id)["data"]
    assert saved_previs["workSettings"] == settings
    project_id = str(uuid4())
    saved_previs["remoteProjects"] = [project_id]
    service.store.checkpoint(saved_previs)

    payload = {
        "requestId": str(uuid4()), "provider": "fake", "model": "fake-video",
        "source": {"jobId": previs_id, "mediaIndex": 0}, "prompt": "A cat follows a butterfly.",
        "duration": 10, "resolution": "480P",
        "referenceMode": "video", "characterMotion": "independent",
        "chatModel": "gpt-6.1-terra", "db_name": "changed", "chatbotid": "changed",
    }
    video = client.post("/api/video/generate", json=payload)
    assert video.status_code == 202, video.text
    options = manager.store.get(payload["requestId"])["options"]
    assert options["workSettings"] == settings
    assert options["chatModel"] == settings["model"]
    assert options["model"] == "fake-video"
    assert options["region"] == "beijing"
    assert options["sceneProjectId"] == project_id
    assert video_analysis_model(service, options) == settings["model"]
    service.config["VIDEO_REGION"] = "singapore"
    retry = client.post("/api/video/generate", json={**payload, "db_name": "changed-again"})
    assert retry.status_code == 202, retry.text
    assert retry.json()["options"]["workSettings"] == settings
    assert retry.json()["options"]["region"] == "beijing"
    assert "프리비즈 생성 요청 접수" in caplog.text
    assert f"[작업 {previs_id}] 프리비즈 실행 대기" in caplog.text
    assert f"[작업 {payload['requestId']}] 영상 생성 요청 처리 완료" in caplog.text
    assert "A cat follows a butterfly." not in caplog.text
