import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlparse

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .agent import JutsuAgent
from .ai_usage import report_usage
from .store import JobStore
from .usage_history import UsageHistory
from .repositories import FEATURE_ROOT, local_repositories, runtime_base
from .pipeline_log import log_message, log_request, logger as pipeline_logger
from .prompt_filter import filter_user_prompts, PromptFilterError
from .shortform_routes import create_shortform_router
from .work_settings import request_work_metadata
from .video_prompt import timeline_duration
from .media_archive import MediaArchive, MediaExpired, ArchiveError, CLEANUP_INTERVAL

DEFAULT_ROOT = FEATURE_ROOT
UUID = r"[a-f0-9-]{36}"
CONSOLE_ASSETS = (
    "api-client.js",
    "video-validation.js",
    "usage-history.js",
    "previs-console.js",
    "shortform.js",
    "wan-upload.html",
    "wan-upload.js",
    "wan-upload.css",
)

DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:9000",
    "http://localhost:9000",
    "http://[::1]:9000",
    "http://127.0.0.1:4180",
    "http://localhost:4180",
)
DEFAULT_CORS_ORIGIN_REGEX = r"https://(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*han\.kr"


class Service:
    def __init__(self, root, python_client=None, agent=None, transport=None, repositories=None):
        self.root = Path(root)
        config = {
            **dotenv_values(self.root.parent.parent / ".env"),
            **dotenv_values(self.root / ".env"),
            **os.environ,
        }
        self.config = config
        try:
            self.max_concurrent_jobs = int(config.get("CONSOLE_MAX_CONCURRENT_JOBS", "2"))
        except (TypeError, ValueError):
            raise ValueError(
                "CONSOLE_MAX_CONCURRENT_JOBS must be an integer from 1 to 64."
            ) from None
        if not 1 <= self.max_concurrent_jobs <= 64:
            raise ValueError("CONSOLE_MAX_CONCURRENT_JOBS must be an integer from 1 to 64.")
        self.frontend = config.get(
            "JUTSU_FRONTEND_URL", "https://api-aipro.chatbaram.com/v1/previs/previs-console01.html"
        )
        redirect = config.get(
            "JUTSU_OAUTH_REDIRECT_URL",
            "https://api-aipro.chatbaram.com/v1/previs/api/jutsu/callback",
        )
        self.origins = {
            urlparse(url).scheme + "://" + urlparse(url).netloc
            for url in (self.frontend, redirect, config.get("PUBLIC_ORIGIN") or self.frontend)
        }
        self.origins.update(DEFAULT_CORS_ORIGINS)
        self.origins.update(
            origin.strip()
            for origin in config.get("CORS_ORIGINS", "").split(",")
            if origin.strip() and origin.strip() != "*"
        )
        self.origin_pattern = re.compile(
            "(?:"
            + DEFAULT_CORS_ORIGIN_REGEX
            + ")|(?:"
            + config.get("CORS_ORIGIN_REGEX", DEFAULT_CORS_ORIGIN_REGEX)
            + ")"
        )
        self.hosts = {urlparse(url).hostname for url in self.origins}
        if python_client is None:
            from ai_video.jutsu_auth import create_client

            python_client = create_client(
                redirect, data_directory=config.get("JUTSU_AUTH_DATA_DIR") or None
            )
        self.client = python_client
        self.default_model = (
            config.get("PREVIS_DEFAULT_CHAT_MODEL", "gpt-6.1-terra").strip() or "gpt-6.1-terra"
        )
        configured_models = [
            model.strip()
            for model in config.get(
                "PREVIS_CHAT_MODELS", "gpt-6.1-terra,gpt-4.1,gpt-4.1-mini"
            ).split(",")
            if model.strip()
        ]
        self.chat_models = list(dict.fromkeys([self.default_model, *configured_models]))
        self.agent = agent or JutsuAgent(
            python_client, config.get("OPENAI_API_KEY"), self.default_model, transport
        )
        self.repositories = repositories if repositories is not None else local_repositories(self.root, transport=transport)
        self.store = self.repositories.previs_jobs
        self.tasks = set()
        self._auth_check_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(self.max_concurrent_jobs)
        self.transport = transport
        self.archive = self.repositories.archive
        self.usage_history = self.repositories.history

    async def ensure_authentication(self):
        """Restore once through the backend, including older transport implementations."""
        async with self._auth_check_lock:
            if self.client.status["state"] == "disconnected":
                self.client.status.update(
                    state="connecting",
                    message="저장된 인증으로 Higgsfield 연결을 확인하고 있습니다.",
                )
            try:
                checker = getattr(self.client, "check_connection", None)
                if checker is not None:
                    await checker()
                else:
                    await self.client.start()
            except Exception:
                self.client.status.update(
                    state="disconnected", message="Higgsfield 연결을 자동으로 다시 확인합니다."
                )
                raise

    async def maintain_authentication(self):
        """Runs independently of all browsers for the lifetime of this API process."""
        logger = logging.getLogger("uvicorn.error")
        previous = None
        while True:
            state = "disconnected"
            try:
                await self.ensure_authentication()
                state = self.client.status["state"]
                marker = (state, self.client.status.get("message"))
                if marker != previous:
                    logger.info(
                        "[previs-auth] state=%s %s", state, self.client.status.get("message", "")
                    )
                    previous = marker
            except asyncio.CancelledError:
                raise
            except Exception as error:
                marker = ("check_error", type(error).__name__)
                if marker != previous:
                    logger.warning(
                        "[previs-auth] 인증 상태 점검 재시도 예정 (%s)", type(error).__name__
                    )
                    previous = marker
            await asyncio.sleep(1800 if state == "connected" else 5)

    async def maintain_media(self):
        while True:
            try:
                await asyncio.to_thread(self.archive.cleanup)
            except (OSError, sqlite3.Error, ArchiveError):
                logging.getLogger(__name__).exception("Console result retention cleanup failed")
            await asyncio.sleep(CLEANUP_INTERVAL)

    async def execute(self, job, history):
        tracker = getattr(self.store, "track_execution", None)
        if tracker is not None:
            tracker(job["id"], asyncio.current_task())
        started = time.monotonic()
        logger = logging.getLogger("uvicorn.error")

        def safe_event(kind, value):
            try:
                self.store.emit(job["id"], kind, value)
                if kind == "ai_usage":
                    self.usage_history.record_usage(job["id"], value)
                elif kind == "checkpoint":
                    self.usage_history.save_previs(job, "running")
            except Exception as error:
                logger.warning("job=%s 진행 로그 저장 지연 (%s)", job["id"], type(error).__name__)

        def timing(label, seconds):
            line = f"[previs-timing] {label}: {seconds:.2f}초"
            logger.info("job=%s %s", job["id"], line)
            safe_event("log", line)

        async def heartbeat():
            while True:
                try:
                    self.store.heartbeat(job["id"])
                except Exception as error:
                    logger.warning(
                        "job=%s 생존 신호 갱신 재시도 예정 (%s)", job["id"], type(error).__name__
                    )
                await asyncio.sleep(15)

        pulse = asyncio.create_task(heartbeat())
        try:
            async with self.slots:
                timing("실행 슬롯 대기", time.monotonic() - started)

                def emit(kind, value):
                    if kind == "checkpoint":
                        try:
                            self.store.checkpoint(job)
                        except Exception as error:
                            logger.warning(
                                "job=%s 진행 상태 저장 재시도 예정 (%s)",
                                job["id"],
                                type(error).__name__,
                            )
                        return
                    if (
                        kind == "log"
                        and isinstance(value, str)
                        and value.startswith("[previs-timing]")
                    ):
                        logger.info("job=%s %s", job["id"], value)
                    safe_event(kind, value)

                agent_started = time.monotonic()
                logger.info("[작업 %s] 프리비즈 생성 시작", job["id"])
                try:
                    output = await self.agent.run(job, history, emit)
                finally:
                    timing("AI 및 원격 씬 작업 전체", time.monotonic() - agent_started)
                if job["media"]:
                    logger.info("[작업 %s] 프리비즈 결과 저장 중 · 파일 %d개", job["id"], len(job["media"]))
                    safe_event(
                        "log", "결과 파일을 서버에 저장합니다. 보관기간은 생성 완료 후 3일입니다."
                    )
                archived = []
                for index, item in enumerate(job["media"]):
                    save_started = time.monotonic()
                    try:
                        archived.append(await self.archive.save(job["id"], index, item))
                    finally:
                        timing(f"결과 파일 {index + 1} 저장", time.monotonic() - save_started)
                job["media"] = archived
                job["media"] = self.archive.complete(job["id"], job["media"])
                try:
                    self.archive.save_scene_reference(job)
                except (OSError, ValueError, sqlite3.Error, ArchiveError):
                    safe_event("log", "3D 장면 연결 정보 보관을 완료하지 못했습니다. 프로젝트 주소를 함께 보관하세요.")
                output["media"] = job["media"]
            output["media"] = [
                {
                    "name": item["name"],
                    "mediaType": item["mediaType"],
                    "url": f"/api/jobs/{job['id']}/media/{index}",
                    "expiresAt": item["archive"]["expiresAt"],
                    **(
                        {
                            "videoSource": {"jobId": job["id"], "mediaIndex": index},
                            "wanSource": {"jobId": job["id"], "mediaIndex": index},
                            "scenePrompt": job.get("scenePrompt", ""),
                            "sourceDuration": job["duration"],
                            **({"workSettings": job["workSettings"]} if job.get("workSettings") else {}),
                        }
                        if item["mediaType"] == "video/mp4"
                        else {}
                    ),
                }
                for index, item in enumerate(output["media"])
            ]
            output.update(
                sceneId=job.get("sceneId"),
                version=job.get("version"),
                baseVersion=job.get("baseVersion"),
            )
            self.store.finish(job, output)
        except asyncio.CancelledError:
            logger.warning("[작업 %s] 프리비즈 생성 중단 · 서버 종료", job["id"])
            self.store.finish(
                job,
                error="서버가 종료되어 작업을 중단했습니다. 마지막 operationId를 확인하세요. 자동 재요청하지 않습니다.",
            )
            raise
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            logger.error("[작업 %s] 프리비즈 생성 실패 · %s · %s",
                         job["id"], type(exc).__name__, log_message(message))
            self.store.finish(job, error=message)
        finally:
            try:
                snapshot = self.store.get(job["id"])
                if snapshot:
                    if snapshot["state"] == "completed":
                        logger.info("[작업 %s] 프리비즈 생성 완료", job["id"])
                    self.usage_history.save_previs(snapshot["data"], snapshot["state"])
            except (OSError, ValueError, sqlite3.Error):
                logger.warning("job=%s 사용기록 저장을 완료하지 못했습니다.", job["id"])
            logger.info(
                "job=%s [previs-timing] 전체 처리: %.2f초", job["id"], time.monotonic() - started
            )
            pulse.cancel()
            try:
                await pulse
            except asyncio.CancelledError:
                pass

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.close()


def create_api_router(*, root=DEFAULT_ROOT, python_client=None, agent=None, transport=None, repositories=None):
    service = Service(root, python_client, agent, transport, repositories)

    @asynccontextmanager
    async def lifespan(app):
        maintenance = asyncio.create_task(service.maintain_media())
        service.tasks.add(maintenance)
        authentication = asyncio.create_task(service.maintain_authentication())
        service.tasks.add(authentication)
        for job in video_router.manager.store.unfinished():
            video_router.manager.ensure(job)
        try:
            yield
        finally:
            await service.close()

    async def validate_origin(request: Request):
        origin = request.headers.get("origin")
        if (
            origin
            and origin not in service.origins
            and not service.origin_pattern.fullmatch(origin)
        ):
            raise HTTPException(403, detail="허용되지 않은 요청 출처입니다.")

    router = APIRouter(lifespan=lifespan, dependencies=[Depends(validate_origin)])
    router.service = service
    router.include_router(create_shortform_router(service))
    from .video_generation import create_video_router

    video_router = create_video_router(service)
    router.include_router(video_router)
    router.video = video_router.manager
    router.wan = router.video

    @router.get("/api/jutsu/status")
    async def status():
        # Initial/restoration requests must not keep returning untouched cached state.
        # The background task still owns periodic checks; healthy reads do not ping.
        if service.client.status["state"] == "disconnected":
            try:
                await service.ensure_authentication()
            except (TimeoutError, RuntimeError):
                pass
        return {
            **service.client.status,
            "managedBy": "backend",
            "authenticationRequired": service.client.status["state"] == "authorizing",
        }

    @router.get("/api/jutsu/connect")
    async def connect():
        try:
            url = await service.client.authorize()
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(
                503, detail=str(exc) or "Higgsfield 인증 연결 시간이 초과되었습니다."
            ) from None
        return RedirectResponse(url or service.frontend, status_code=302)

    @router.post("/api/jutsu/connect-browser")
    async def connect_browser():
        try:
            url = await service.client.authorize()
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(
                503, detail=str(exc) or "Higgsfield 인증 연결 시간이 초과되었습니다."
            ) from None
        # Browser navigation belongs to the frontend, never a server-side Node helper.
        return {"opened": False, "authorizationUrl": url, "status": service.client.status}

    @router.get("/api/jutsu/callback")
    async def callback(request: Request):
        if request.query_params.get("error"):
            raise HTTPException(
                400, detail="Higgsfield 인증이 취소되거나 거절되었습니다. 다시 연결하세요."
            )
        try:
            service.client.storage.answer(
                request.query_params.get("code"), request.query_params.get("state")
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from None
        return RedirectResponse(service.frontend, status_code=302)

    @router.get("/api/chat/models")
    async def chat_models():
        return {"models": service.chat_models, "default": service.default_model}

    @router.post("/api/chat", status_code=202)
    @log_request("프리비즈 생성")
    async def chat(request: Request):
        if len(await request.body()) > 16384:
            raise HTTPException(413, detail="입력이 너무 깁니다.")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, detail="JSON 요청 형식이 올바르지 않습니다.") from None
        if not isinstance(body, dict) or not isinstance(body.get("message"), str):
            raise HTTPException(400, detail="장면 내용을 입력하세요.")
        message = body["message"].strip()
        if not message or len(message) > 8000:
            raise HTTPException(400, detail="장면 내용은 1~8000자로 입력하세요.")
        model = body.get("model", service.default_model)
        if not isinstance(model, str) or model not in service.chat_models:
            raise HTTPException(400, detail="허용된 채팅 모델을 선택하세요.")
        try:
            work_metadata = request_work_metadata(body)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        session_id = body.get("sessionId")
        if session_id is not None and (
            not isinstance(session_id, str) or not re.fullmatch(UUID, session_id)
        ):
            raise HTTPException(400, detail="sessionId 형식이 올바르지 않습니다.")
        try:
            duration = int(body.get("duration", 5))
        except (ValueError, TypeError):
            duration = 5
        duration = duration if duration in (5, 10, 15) else 5
        scenario_titles = body.get("scenarioTitles")
        if scenario_titles is not None:
            if (not isinstance(scenario_titles, list) or not 1 <= len(scenario_titles) <= 40
                    or not all(isinstance(title, str) and 1 <= len(title.strip()) <= 120
                               for title in scenario_titles)):
                raise HTTPException(400, detail="시나리오 씬 목록을 확인하세요.")
            scenario_titles = [title.strip() for title in scenario_titles]
        explicit_duration = timeline_duration(message) if scenario_titles is None else None
        if explicit_duration in (5, 10, 15):
            duration = explicit_duration
        aspect = body.get("aspect", "16:9")
        aspect = aspect if aspect in ("1:1", "3:4", "4:3", "9:16", "16:9") else "16:9"
        inspection_usage = []
        try:
            new_scene = body.get("newScene", False)
            if type(new_scene) is not bool:
                raise HTTPException(400, detail="newScene은 boolean이어야 합니다.")
            new_scene = new_scene or bool(
                re.match(
                    r"^(?:새로운?\s*장면|새\s*장면|new\s+scene)\s*(?:생성|시작|만들|:|$)",
                    message,
                    re.I,
                )
            )
            baseline_prompt = service.store.prompt_context(session_id, new_scene=new_scene)
            pipeline_logger.info("[요청 %s] 프리비즈 프롬프트 검사 시작", request.state.pipeline_id)
            prompt_filter = await filter_user_prompts(
                baseline_prompt, message, model=model, api_key=service.config.get("OPENAI_API_KEY"),
                transport=service.transport, usage_sink=inspection_usage.append,
            )
            job, session, history = service.store.create(
                message, session_id, duration, aspect, new_scene=new_scene,
                **({"scenario_titles": scenario_titles} if scenario_titles else {}),
                **({"work_settings": {"model": model, **work_metadata}} if work_metadata else {}),
            )
        except PromptFilterError as error:
            raise HTTPException(error.status_code, detail=str(error)) from None
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        job["chatModel"] = model
        job["sessionId"] = session
        job["promptFilter"] = prompt_filter
        try:
            service.usage_history.save_previs(job, "running")
        except (OSError, ValueError, sqlite3.Error):
            logging.getLogger("uvicorn.error").warning("job=%s 사용기록 저장 지연", job["id"])

        def save_inspection_usage(value):
            service.store.emit(job["id"], "ai_usage", value)
            service.usage_history.record_usage(job["id"], value)

        for row in inspection_usage:
            report_usage(save_inspection_usage,
                         {**row, "job_id": job["id"], "session_id": session})
        pipeline_logger.info("[요청 %s] [작업 %s] 프리비즈 실행 대기 · 모델 %s",
                             request.state.pipeline_id, job["id"], model)
        task = asyncio.create_task(service.execute(job, history))
        tracker = getattr(service.store, "track_execution", None)
        if tracker is not None:
            tracker(job["id"], task)
        service.tasks.add(task)
        task.add_done_callback(service.tasks.discard)
        return {
            "jobId": job["id"],
            "sessionId": session,
            "duration": duration,
            "sceneId": job["sceneId"],
            "version": job["version"],
            "baseVersion": job["baseVersion"],
        }

    @router.get("/api/sessions/{session_id}/versions")
    async def scene_versions(session_id: str):
        if not re.fullmatch(UUID, session_id):
            raise HTTPException(400, detail="세션 ID 형식이 올바르지 않습니다.")
        return {"versions": service.store.versions(session_id)}

    @router.get("/api/jobs/{job_id}/source")
    async def version_source(job_id: str):
        data = service.store.get(job_id)
        if data is None:
            raise HTTPException(404, detail="작업을 찾을 수 없습니다.")
        job = data["data"]
        return {
            key: job.get(key)
            for key in (
                "sceneId",
                "version",
                "baseVersion",
                "request",
                "plan",
                "workerDrafts",
                "repairHistory",
                "cameraScale",
                "cameraMetrics",
                "cameraQuality",
                "codeOperations",
                "codeValidation",
                "remoteProjects",
                "reviewBudget",
                "finalization",
                "spatialManifestRef",
            )
        }

    @router.get("/api/jobs/{job_id}")
    async def job(job_id: str, after: int = -1):
        data = service.store.get(job_id, after)
        if data is None:
            raise HTTPException(404, detail="작업을 찾을 수 없습니다.")
        events = data["events"]
        return {"state": data["state"], "events": events,
                "ai_usage": [event["value"] for event in events if event["type"] == "ai_usage"]}

    @router.get("/api/usage-history")
    async def usage_history(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), hidden: bool = False):
        return await asyncio.to_thread(service.usage_history.list_records, service, video_router.manager,
                                       offset=offset, limit=limit, hidden=hidden)

    @router.get("/api/usage-history/{job_id}")
    async def usage_history_details(job_id: str):
        if not re.fullmatch(UUID, job_id):
            raise HTTPException(404, detail="사용기록을 찾을 수 없습니다.")
        data = await asyncio.to_thread(service.usage_history.details, job_id, service, video_router.manager)
        if data is None:
            raise HTTPException(404, detail="사용기록을 찾을 수 없습니다.")
        return data

    @router.delete("/api/usage-history/{job_id}")
    async def delete_usage_history(job_id: str):
        if not re.fullmatch(UUID, job_id):
            raise HTTPException(404, detail="사용기록을 찾을 수 없습니다.")
        await asyncio.to_thread(service.usage_history.delete, job_id)
        return {"ok": True, "jobId": job_id}

    @router.post("/api/usage-history/{job_id}/visibility")
    async def usage_history_visibility(job_id: str, request: Request):
        if not re.fullmatch(UUID, job_id):
            raise HTTPException(404, detail="사용기록을 찾을 수 없습니다.")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(422, detail="hidden 값을 true 또는 false로 전달하세요.")
        if not isinstance(payload, dict) or type(payload.get("hidden")) is not bool:
            raise HTTPException(422, detail="hidden 값을 true 또는 false로 전달하세요.")
        found = await asyncio.to_thread(service.usage_history.set_hidden, job_id, payload["hidden"])
        if not found:
            raise HTTPException(404, detail="사용기록을 찾을 수 없습니다.")
        return {"ok": True, "jobId": job_id, "hidden": payload["hidden"]}

    @router.get("/api/recovered")
    async def recovered():
        results = []
        directory = runtime_base(service.root) / "runtime/recovered"
        for file in sorted(directory.glob("*.json")):
            if not re.fullmatch(UUID, file.stem):
                continue
            try:
                data = json.loads(file.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError):
                continue
            media = []
            for ext, kind in (("mp4", "video/mp4"), ("blend", "application/x-blender")):
                path = directory / (file.stem + "." + ext)
                if path.is_symlink():
                    continue
                try:
                    asset = service.archive.track(
                        "recovered-" + file.stem + "-" + ext, path, created=path.stat().st_mtime
                    )
                    service.archive.path(asset["key"])
                except (MediaExpired, FileNotFoundError):
                    continue
                media.append(
                    {
                        "name": "previs." + ext,
                        "mediaType": kind,
                        "url": "/api/recovered/" + file.stem + "." + ext,
                        "expiresAt": asset["expiresAt"],
                        **(
                            {
                                "videoSource": {"recoveredId": file.stem},
                                "wanSource": {"recoveredId": file.stem},
                            }
                            if ext == "mp4"
                            else {}
                        ),
                    }
                )
                if ext == "mp4":
                    media[-1]["scenePrompt"] = (
                        data.get("scenePrompt", "")
                        if isinstance(data.get("scenePrompt", ""), str)
                        else ""
                    )
                    media[-1]["sourceDuration"] = data.get("durationSeconds", 5)
            if media:
                results.append(
                    {
                        "message": f"복구된 프리비즈가 준비되었습니다. {data.get('durationSeconds', '')}초 · {data.get('fps', 24)}fps",
                        "media": media,
                    }
                )
        return {"results": results}

    @router.get("/api/recovered/{filename}")
    async def recovered_file(filename: str, download: bool = False):
        if not re.fullmatch(UUID + r"\.(mp4|blend)", filename):
            raise HTTPException(404, detail="결과물이 없습니다.")
        file = runtime_base(service.root) / "runtime/recovered" / filename
        key = "recovered-" + filename.rsplit(".", 1)[0] + "-" + file.suffix[1:]
        record = service.archive.record(key)
        if record and time.time() >= record["expiresAt"]:
            raise HTTPException(410, detail=str(MediaExpired()))
        if not file.is_file() or file.is_symlink():
            raise HTTPException(404, detail="결과물이 없습니다.")
        asset = service.archive.track(key, file, created=file.stat().st_mtime)
        try:
            file = service.archive.path(asset["key"])
        except MediaExpired as error:
            raise HTTPException(410, detail=str(error)) from None
        return FileResponse(
            file,
            media_type="video/mp4" if file.suffix == ".mp4" else "application/x-blender",
            filename=f"previs-{Path(filename).stem}{file.suffix}",
            content_disposition_type="attachment" if download else "inline",
            headers={"cache-control": "no-store"},
        )

    @router.get("/api/jobs/{job_id}/media/{index}")
    async def media(job_id: str, index: int, request: Request, download: bool = False):
        job = service.store.get(job_id)
        if job is None:
            try:
                archived = service.archive.previs(job_id, index)
            except MediaExpired as error:
                raise HTTPException(410, detail=str(error)) from None
            except FileNotFoundError:
                raise HTTPException(404, detail="저장된 결과 파일이 없습니다.") from None
            except ArchiveError as error:
                raise HTTPException(502, detail=str(error)) from None
            return FileResponse(
                archived["path"],
                media_type="video/mp4",
                filename=f"previs-{job_id}-{index}.mp4",
                content_disposition_type="attachment" if download else "inline",
                headers={"cache-control": "no-store"},
            )
        if index < 0 or index >= len(job["data"]["media"]):
            raise HTTPException(404, detail="결과물이 없습니다.")
        item = job["data"]["media"][index]
        try:
            if "archive" not in item:
                # Older jobs can be archived on first access, within their
                # original retention window. Reading never resets the clock.
                from datetime import datetime

                completed = next(
                    (
                        event["time"]
                        for event in reversed(job["events"])
                        if event["type"] == "complete"
                    ),
                    None,
                )
                created = datetime.fromisoformat(completed).timestamp() if completed else 0
                item = await service.archive.save(job_id, index, item, created=created)
                service.store.replace_media(job_id, index, item)
            path = service.archive.path(item["archive"]["key"])
        except MediaExpired as error:
            raise HTTPException(410, detail=str(error)) from None
        except FileNotFoundError:
            raise HTTPException(404, detail="저장된 결과 파일이 없습니다.") from None
        except ArchiveError as error:
            raise HTTPException(502, detail=str(error)) from None
        extension = ".mp4" if item["mediaType"] == "video/mp4" else ".blend"
        return FileResponse(
            path,
            media_type=item["mediaType"],
            filename=f"previs-{job_id}-{index}{extension}",
            content_disposition_type="attachment" if download else "inline",
            headers={"cache-control": "no-store"},
        )

    return router


def create_app(**options):
    router = create_api_router(**options)
    app = FastAPI()
    app.state.video = router.video
    app.state.wan = router.video
    app.include_router(router)

    @app.middleware("http")
    async def check_host(request: Request, call_next):
        if request.url.hostname not in router.service.hosts:
            return JSONResponse({"detail": "허용되지 않은 호스트입니다."}, status_code=403)
        return await call_next(request)

    public = router.service.root / "public"
    files = {
        "/": "previs-console01.html",
        "/previs-console01.html": "previs-console01.html",
        **{"/" + name: name for name in CONSOLE_ASSETS},
        "/higgsfield-previs-console.html": "higgsfield-previs-console.html",
        "/higgsfield-previs-console.js": "higgsfield-previs-console.js",
        "/higgsfield-previs-console.css": "higgsfield-previs-console.css",
        "/higgsfield-previs-config.js": "higgsfield-previs-config.js",
    }

    @app.get("/{path:path}")
    async def static(path: str):
        filename = files.get("/" + path)
        if not filename or not (public / filename).is_file():
            raise HTTPException(404, detail="페이지를 찾을 수 없습니다.")
        return FileResponse(
            public / filename,
            headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
        )

    return app
