"""Standalone video generation and Python previs service (no chat app/Node)."""

import logging
import os
from pathlib import Path
import re

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute

load_dotenv(override=False)
ROOT = Path(__file__).resolve().parent
FEATURE_ROOT = ROOT / "ai_video/video_higgsfield_jutsu"


class ChatPollingAccessFilter(logging.Filter):
    status_paths = re.compile(
        r"/(?:AI_video/jutsu/)?api/(?:video/)?jobs/"
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
        r"|/AI_video/jobs/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
        r"|/(?:AI_video/jutsu/)?api/usage-history",
        re.IGNORECASE,
    )

    media_paths = re.compile(
        r"/(?:AI_video/jutsu/)?api/(?:jobs/[^/]+/media/[0-9]+"
        r"|video/jobs/[^/]+/(?:video|guides/[0-9]+|scenario-snapshots/[0-9]+))"
        r"|/AI_video/jobs/[^/]+/media/[0-9]+"
    )
    history_delete_path = re.compile(
        r"/(?:AI_video/jutsu/)?api/usage-history/(?P<job_id>[^/]+)"
    )

    def filter(self, record):
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, target, _, status = args
        if not isinstance(target, str):
            return True
        path = target.partition("?")[0]
        if method == "GET":
            if status in (200, 204) and self.status_paths.fullmatch(path):
                return False
            if status in (200, 206) and self.media_paths.fullmatch(path):
                return False
        if method == "DELETE" and status == 200 and self.history_delete_path.fullmatch(path):
            return False
        return True


chat_polling_access_filter = ChatPollingAccessFilter()


def create_app(*, video_router=None, python_client=None, agent=None, previs_root=None, repositories=None):
    logging.getLogger("uvicorn.access").addFilter(chat_polling_access_filter)
    # Import only the generated generation router, never the full chat/video app.
    from ai_video.video_higgsfield_jutsu.python_backend.app import (
        create_api_router,
        DEFAULT_CORS_ORIGIN_REGEX,
        DEFAULT_CORS_ORIGINS,
        CONSOLE_ASSETS,
    )

    app = FastAPI(title="AI Video Previs")
    origins = [
        value.strip()
        for value in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:9000,http://localhost:9000,http://127.0.0.1:4180,http://localhost:4180,https://dev.han.kr,https://php8.han.kr",
        ).split(",")
        if value.strip()
    ]
    origins = list(dict.fromkeys([*DEFAULT_CORS_ORIGINS, *origins]))
    origin_regex = (
        "(?:"
        + DEFAULT_CORS_ORIGIN_REGEX
        + ")|(?:"
        + os.getenv("CORS_ORIGIN_REGEX", DEFAULT_CORS_ORIGIN_REGEX)
        + ")"
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_origin_regex=origin_regex,
        allow_headers=["Content-Type", "Range", "sessionId"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Disposition"],
    )

    previs = create_api_router(
        root=previs_root or FEATURE_ROOT, python_client=python_client, agent=agent, repositories=repositories
    )
    app.include_router(previs)
    clean_paths = {
        "/api/jutsu/status": "/AI_video/jutsu/status",
        "/api/jutsu/connect": "/AI_video/jutsu/connect",
        "/api/jutsu/connect-browser": "/AI_video/jutsu/connect-browser",
        "/api/jutsu/callback": "/AI_video/jutsu/callback",
        "/api/chat": "/AI_video/jutsu/previs",
        "/api/jobs/{job_id}": "/AI_video/jobs/{job_id}",
        "/api/jobs/{job_id}/media/{index}": "/AI_video/jobs/{job_id}/media/{index}",
        "/api/recovered": "/AI_video/results",
        "/api/recovered/{filename}": "/AI_video/results/{filename}",
    }
    # Register aliases to the same service/store. Include its lifespan only once.
    for route in previs.routes:
        if isinstance(route, APIRoute):
            for path in ("/AI_video/jutsu" + route.path, clean_paths.get(route.path)):
                if path:
                    app.router.add_api_route(
                        path,
                        route.endpoint,
                        methods=sorted(route.methods),
                        status_code=route.status_code,
                        dependencies=route.dependencies,
                        response_model=route.response_model,
                        include_in_schema=True,
                        route_class_override=type(route),
                    )

    @app.get("/health", include_in_schema=False)
    async def health():
        # Process health only. Does not start authentication or call providers/DB.
        return {"status": "ok", "runtime": "python"}

    public = FEATURE_ROOT / "public"

    @app.get("/", include_in_schema=False)
    @app.get("/jutsu-remote.html", include_in_schema=False)
    @app.get("/previs-console01.html", include_in_schema=False)
    async def console():
        return FileResponse(public / "previs-console01.html", headers={"Cache-Control": "no-store"})

    for name in CONSOLE_ASSETS:

        def asset_endpoint(filename):
            async def serve():
                return FileResponse(public / filename, headers={"Cache-Control": "no-store"})

            return serve

        app.add_api_route(
            "/" + name, asset_endpoint(name), methods=["GET"], include_in_schema=False
        )

    return app
