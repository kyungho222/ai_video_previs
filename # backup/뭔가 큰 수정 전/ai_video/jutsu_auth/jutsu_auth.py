"""Higgsfield OAuth storage and MCP transport for the authentication package."""

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

ENDPOINT = "https://mcp.higgsfield.ai/mcp"
TOOL_DISCOVERY_TIMEOUT_SECONDS = 180
TOOL_PATTERN = re.compile(
    r"(?:^|_)scene_builder_3d_(create_project|list_projects|get_project|query_python|"
    r"run_python|get_operation|get_artifact|get_blend|get_glb|search_assets|import_asset|show_scene)$"
)
WORKFLOW_TOOL_PATTERN = re.compile(r"(?:^|_)get_preset_instructions$")


class OAuthStorage:
    """Private SQLite storage; callbacks may be served by a different worker."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS credentials (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending (state TEXT PRIMARY KEY, expires REAL, code TEXT, answered INTEGER DEFAULT 0)"
            )
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self, name):
        with self.connect() as db:
            row = db.execute("SELECT value FROM credentials WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, name, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO credentials VALUES (?, ?)", (name, json.dumps(value))
            )

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        data = self.load("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens):
        self.save("tokens", tokens.model_dump(mode="json"))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        data = self.load("client")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info):
        self.save("client", client_info.model_dump(mode="json"))

    def begin(self, state, lifetime=300):
        if not state:
            raise ValueError("OAuth state가 없습니다.")
        with self.connect() as db:
            db.execute("DELETE FROM pending WHERE expires < ?", (time.time(),))
            db.execute(
                "INSERT INTO pending(state,expires) VALUES (?,?)", (state, time.time() + lifetime)
            )

    def answer(self, code, state):
        if not code or not state:
            raise ValueError("인증 code와 state가 필요합니다. 다시 연결하세요.")
        with self.connect() as db:
            changed = db.execute(
                "UPDATE pending SET code=?, answered=1 WHERE state=? AND expires>? AND answered=0",
                (code, state, time.time()),
            ).rowcount
            if changed != 1:
                raise ValueError(
                    "인증 state가 일치하지 않거나 만료·사용되었습니다. 다시 연결하세요."
                )

    def take(self, state):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT code FROM pending WHERE state=? AND expires>? AND answered=1",
                (state, time.time()),
            ).fetchone()
            if row:
                db.execute("DELETE FROM pending WHERE state=?", (state,))
                return row[0], state
        return None


class JutsuPythonClient:
    def __init__(self, storage_path, redirect_url):
        self.storage = OAuthStorage(storage_path)
        self.redirect_url = redirect_url
        self.definitions = []
        self.status = {
            "state": "disconnected",
            "message": "Higgsfield 인증이 필요합니다.",
            "endpoint": ENDPOINT,
            "tools": [],
        }
        self._task = None
        self._ready = asyncio.Event()
        self._calls = asyncio.Queue()
        self._authorization_url = None
        self._active_call = None
        self._health_lock = asyncio.Lock()

    async def _redirect(self, url):
        state = parse_qs(urlparse(url).query).get("state", [None])[0]
        self.storage.begin(state)
        self._authorization_url = url
        self.status.update(
            state="authorizing", message="Higgsfield 로그인 완료를 기다리고 있습니다."
        )
        self._ready.set()

    async def _callback(self):
        state = parse_qs(urlparse(self._authorization_url).query)["state"][0]
        try:
            async with asyncio.timeout(300):
                while True:
                    answer = self.storage.take(state)
                    if answer:
                        return answer
                    await asyncio.sleep(0.2)
        finally:
            self._authorization_url = None

    async def _run(self):
        try:
            from mcp import ClientSession
            from mcp.client.auth import OAuthClientProvider
            from mcp.client.streamable_http import streamablehttp_client
            from mcp.shared.auth import OAuthClientMetadata
            from pydantic import AnyUrl

            provider = OAuthClientProvider(
                server_url=ENDPOINT,
                client_metadata=OAuthClientMetadata(
                    client_name="3D Jutsu Previs Console Python",
                    redirect_uris=[AnyUrl(self.redirect_url)],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                ),
                storage=self.storage,
                redirect_handler=self._redirect,
                callback_handler=self._callback,
            )
            # All transport contexts are entered/exited in this owner task.
            async with streamablehttp_client(ENDPOINT, auth=provider) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._authorization_url = None
                    self.status.update(
                        state="connecting",
                        message="Higgsfield 인증 완료. 3D 도구 목록을 확인하고 있습니다.",
                    )
                    self._ready.set()
                    tools, cursor = [], None
                    try:
                        async with asyncio.timeout(TOOL_DISCOVERY_TIMEOUT_SECONDS):
                            while True:
                                page = await session.list_tools(cursor=cursor)
                                tools.extend(page.tools)
                                cursor = page.nextCursor
                                if not cursor:
                                    break
                    except TimeoutError:
                        self.status.update(
                            state="unavailable",
                            message="Higgsfield 인증은 완료됐지만 도구 목록 조회 시간이 초과되었습니다. 잠시 후 다시 연결하세요.",
                            tools=[],
                        )
                        return
                    self.definitions = [
                        t.model_dump(mode="json", by_alias=True)
                        for t in tools
                        if TOOL_PATTERN.search(t.name) or WORKFLOW_TOOL_PATTERN.search(t.name)
                    ]
                    kinds = {
                        match.group(1)
                        for t in self.definitions
                        if (match := TOOL_PATTERN.search(t["name"]))
                    }
                    available = {
                        "create_project",
                        "get_project",
                        "run_python",
                        "get_operation",
                        "get_artifact",
                    } <= kinds and any(
                        WORKFLOW_TOOL_PATTERN.search(t["name"]) for t in self.definitions
                    )
                    self.status.update(
                        state="connected" if available else "unavailable",
                        message=(
                            "Higgsfield 3D Jutsu 연결됨"
                            if available
                            else "인증은 완료됐지만 필요한 3D Jutsu 도구를 제공하지 않습니다."
                        ),
                        tools=[t["name"] for t in self.definitions],
                        availableTools=[t.name for t in tools],
                    )
                    self._authorization_url = None
                    self._ready.set()
                    while True:
                        name, args, future = await self._calls.get()
                        if future.cancelled():
                            continue
                        self._active_call = name
                        try:
                            if name == "__auth_ping__":
                                await session.send_ping()
                                result = {"ok": True}
                            else:
                                result = (await session.call_tool(name, arguments=args)).model_dump(
                                    mode="json", by_alias=True
                                )
                            if not future.done():
                                future.set_result(result)
                        except Exception:
                            if not future.done():
                                future.set_exception(RuntimeError("Higgsfield MCP 도구 호출 실패"))
                            if name == "__auth_ping__":
                                raise
                        finally:
                            self._active_call = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never expose exception bodies that may contain OAuth credentials.
            logging.getLogger(__name__).error(
                "Higgsfield Python connection failed (%s)", type(exc).__name__
            )
            self.status.update(
                state="disconnected",
                message="Python Higgsfield 연결 실패. SDK 설치와 연결 설정을 확인하고 다시 연결하세요.",
            )
            self._authorization_url = None
            self._ready.set()
        finally:
            while not self._calls.empty():
                _, _, future = self._calls.get_nowait()
                if not future.done():
                    future.set_exception(
                        RuntimeError("Higgsfield 연결이 종료되었습니다. 다시 연결하세요.")
                    )

    async def start(self):
        if self._task is None or self._task.done():
            self._ready.clear()
            self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self._ready.wait(), 45)

    async def check_connection(self):
        """Backend-owned restoration and idle-session ping; never replay scene mutations."""
        async with self._health_lock:
            await self.start()
            if self.status["state"] != "connected" or self._active_call or not self._calls.empty():
                return self.status
            future = asyncio.get_running_loop().create_future()
            await self._calls.put(("__auth_ping__", {}, future))
            try:
                await asyncio.wait_for(future, 20)
                self.status["checkedAt"] = time.time()
            except (TimeoutError, RuntimeError):
                # OAuth may be waiting for interactive consent; keep that flow alive.
                if self.status["state"] == "authorizing":
                    return self.status
                if self._active_call == "__auth_ping__":
                    await self.close()
                self.status.update(
                    state="disconnected", message="Higgsfield 연결을 자동으로 복구하고 있습니다."
                )
            return self.status

    async def authorize(self):
        await self.start()
        if self.status["state"] == "disconnected":
            raise RuntimeError(self.status["message"])
        return self._authorization_url

    async def call(self, name, arguments):
        await self.start()
        if self.status["state"] != "connected":
            raise ValueError(self.status["message"])
        if not any(t["name"] == name for t in self.definitions):
            raise ValueError("허용되지 않은 Higgsfield 도구입니다.")
        future = asyncio.get_running_loop().create_future()
        await self._calls.put((name, arguments, future))
        return await asyncio.wait_for(future, 70)

    async def close(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
