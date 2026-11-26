"""Server-owned console results and uploaded source retention."""

from contextlib import contextmanager
from pathlib import Path
import os
import json
import re
import sqlite3
import time
from uuid import uuid4

import httpx

from .video_media import PublicHTTPTransport, public_https
from .video_errors import VideoError
from .retention_policy import is_expired

RETENTION_DAYS = None
RETENTION_SECONDS = None
UPLOAD_RETENTION_SECONDS = None
PARTIAL_RETENTION_SECONDS = 5 * 24 * 60 * 60
CLEANUP_INTERVAL = 10 * 60
UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"


class ArchiveError(RuntimeError):
    """Safe to show in the console: never includes signed URLs or headers."""


class MediaExpired(ArchiveError):
    def __init__(self):
        super().__init__("파일 보관기간이 만료되었습니다.")


class MediaArchive:
    def __init__(self, root, *, transport=None):
        self.root = Path(root).resolve()
        self.directory = self.root / "runtime/archive"
        self.database = self.root / "runtime/data/media-archive.sqlite3"
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.transport = transport
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS media_assets(key TEXT PRIMARY KEY, path TEXT UNIQUE, saved REAL, expires REAL)"
            )

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def safe_path(self, relative):
        relative = Path(relative)
        if (
            relative.is_absolute()
            or len(relative.parts) < 3
            or relative.parts[0] != "runtime"
            or relative.parts[1] not in ("archive", "wan", "recovered", "wan-inputs")
            or ".." in relative.parts
        ):
            raise ArchiveError("저장 파일 경로가 올바르지 않습니다.")
        path = self.root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ArchiveError("저장 파일 경로가 올바르지 않습니다.")
        if not path.parent.resolve().is_relative_to(self.root):
            raise ArchiveError("저장 파일 경로가 올바르지 않습니다.")
        return path

    def record(self, key):
        with self.db() as db:
            row = db.execute(
                "SELECT path,saved,expires FROM media_assets WHERE key=?", (key,)
            ).fetchone()
        if row:
            return {"key": key, "relative": row[0], "savedAt": row[1], "expiresAt": row[2]}

    def track(self, key, path, *, created=None, reuse_path=False):
        if Path(path).is_symlink():
            raise ArchiveError("저장 파일 경로가 올바르지 않습니다.")
        try:
            relative = Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            # On Windows a concurrently deleted file can resolve with a \\?\
            # namespace prefix. Discovery already treats missing files as races.
            if not Path(path).is_file():
                raise FileNotFoundError("저장 파일이 없습니다.") from None
            raise ArchiveError("저장 파일 경로가 올바르지 않습니다.") from None
        self.safe_path(relative)
        with self.db() as db:
            row = db.execute(
                "SELECT key,path,saved,expires FROM media_assets WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                if not Path(path).is_file():
                    raise FileNotFoundError("저장 파일이 없습니다.")
                saved = time.time() if created is None else created
                db.execute(
                    "INSERT OR IGNORE INTO media_assets VALUES (?,?,?,?)",
                    (key, relative, saved, None),
                )
                row = db.execute(
                    "SELECT key,path,saved,expires FROM media_assets WHERE key=?", (key,)
                ).fetchone()
                if row is None and reuse_path:
                    row = db.execute(
                        "SELECT key,path,saved,expires FROM media_assets WHERE path=?", (relative,)
                    ).fetchone()
            if row is None or row[1] != relative:
                raise ArchiveError("저장 파일 식별자가 일치하지 않습니다.")
        return {"key": row[0], "savedAt": row[2], "expiresAt": row[3]}

    def path(self, key, *, now=None):
        record = self.record(key)
        if not record:
            raise FileNotFoundError("저장 파일이 없습니다.")
        if is_expired(record["expiresAt"], now=now):
            raise MediaExpired()
        path = self.safe_path(record["relative"])
        if not path.is_file():
            raise FileNotFoundError("저장 파일이 없습니다.")
        return path

    def save_scene_reference(self, job):
        """Keep only scene IDs/plan with the same retention clock as the MP4."""
        job_id = job["id"]
        if not re.fullmatch(UUID, job_id):
            raise ArchiveError("장면 작업 식별자가 올바르지 않습니다.")
        projects = [value for value in job.get("remoteProjects", [])
                    if isinstance(value, str) and re.fullmatch(UUID, value)]
        if not projects:
            return
        media_projects = {}
        for index, item in enumerate(job.get("media", [])):
            project = ((item.get("artifactRef") or {}).get("arguments") or {}).get("projectId")
            if isinstance(project, str) and re.fullmatch(UUID, project):
                media_projects[str(index)] = project
        data = {"version": 1, "remoteProjects": projects, "mediaProjects": media_projects,
                "sessionId": job.get("sessionId"),
                "request": job.get("request", ""),
                "plan": job.get("plan", {}), "sceneDesign": job.get("sceneDesign"), "sceneState": job.get("sceneState"),
                "sceneAdapter": job.get("sceneAdapter"),
                "sceneAdapterCompletion": job.get("sceneAdapterCompletion"),
                "scenePrompt": job.get("scenePrompt", ""),
                "spatialManifestRef": job.get("spatialManifestRef")}
        if job.get("scenarioTitles"):
            data["scenarioTitles"] = job["scenarioTitles"]
        for field in ('sceneDirection', 'directionAdjustments', 'sceneDesignBeforeCorrection', 'serverSceneCorrection', 'rawSceneDirection', 'rawSceneModelResponse', 'normalizedSceneDirection', 'normalizationChanges', 'canonicalSceneStatus', 'intentQuality', 'cameraQuality'):
            if field in job:
                data[field] = job[field]
        if job.get("workSettings"):
            data["workSettings"] = job["workSettings"]
        target = self.safe_path(Path("runtime/archive") / ("scene-" + job_id + ".json"))
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + "." + uuid4().hex + ".part")
        created = min((item["archive"]["savedAt"] for item in job.get("media", [])
                       if item.get("archive")), default=time.time())
        try:
            partial.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            os.utime(partial, (created, created))
            partial.replace(target)
            self.track("scene-" + job_id, target, created=created)
        finally:
            partial.unlink(missing_ok=True)

    def scene_reference(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(UUID, job_id):
            return {}
        try:
            path = self.path("scene-" + job_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("version") == 1 else {}
        except (ArchiveError, FileNotFoundError, ValueError, OSError):
            return {}

    def previs(self, job_id, index):
        """Resolve an archived MP4 independently of the process-local job store."""
        if (
            not isinstance(job_id, str)
            or not re.fullmatch(UUID, job_id)
            or type(index) is not int
            or index < 0
        ):
            raise FileNotFoundError("저장 파일 식별자가 올바르지 않습니다.")
        key = f"{job_id}-{index}"
        path = self.directory / (key + ".mp4")
        asset = self.track(key, path, created=path.stat().st_mtime)
        return {**asset, "path": self.path(asset["key"]), "jobId": job_id, "mediaIndex": index}

    def previs_results(self):
        """Discover retained MP4s without extending their original retention."""
        results = []
        for path in self.directory.glob("*.mp4"):
            match = re.fullmatch("(" + UUID + r")-(\d+)\.mp4", path.name)
            if not match:
                continue
            try:
                results.append(self.previs(match[1], int(match[2])))
            except (FileNotFoundError, MediaExpired, ArchiveError):
                continue
        return sorted(results, key=lambda item: item["savedAt"], reverse=True)

    async def save(self, job_id, index, item, *, created=None):
        if not re.fullmatch(UUID, job_id) or type(index) is not int or index < 0:
            raise ArchiveError("저장 파일 식별자가 올바르지 않습니다.")
        extensions = {"video/mp4": "mp4", "application/x-blender": "blend"}
        extension = extensions.get(item.get("mediaType"))
        if not extension:
            raise ArchiveError("지원하지 않는 결과 파일 형식입니다.")
        key = job_id + "-" + str(index)
        current = self.record(key)
        if current:
            # Never extend retention just because a result was accessed again.
            if is_expired(current["expiresAt"]):
                raise MediaExpired()
            try:
                self.path(key)
                return {
                    **{k: v for k, v in item.items() if k not in ("download", "archive")},
                    "archive": {name: current[name] for name in ("key", "savedAt", "expiresAt")},
                }
            except FileNotFoundError:
                pass
        target = self.safe_path(Path("runtime/archive") / (key + "." + extension))
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + "." + uuid4().hex + ".part")
        download = item.get("download", {})
        if download.get("method") != "GET":
            raise ArchiveError("생성 결과의 다운로드 정보가 없습니다.")
        try:
            await public_https(download.get("url", ""))
            limit = (500 if extension == "mp4" else 2048) * 1024 * 1024
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300, connect=15),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport or PublicHTTPTransport(),
            ) as http:
                # An archive must always contain the full file, never a Range response.
                headers = {
                    k: v for k, v in download.get("headers", {}).items() if k.lower() != "range"
                }
                async with http.stream("GET", download["url"], headers=headers) as response:
                    if response.status_code != 200:
                        raise ArchiveError(
                            "생성 결과 저장에 실패했습니다. 원본 링크 만료 또는 다운로드 권한을 확인하세요."
                        )
                    expected = response.headers.get("content-length")
                    if expected and (not expected.isdigit() or int(expected) > limit):
                        raise ArchiveError("생성 결과 파일 크기 제한을 초과했습니다.")
                    size = 0
                    with partial.open("xb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise ArchiveError("생성 결과 파일 크기 제한을 초과했습니다.")
                            output.write(chunk)
                    if not size or (
                        expected
                        and not response.headers.get("content-encoding")
                        and size != int(expected)
                    ):
                        raise ArchiveError("생성 결과 파일이 끝까지 저장되지 않았습니다.")
            with partial.open("rb") as content:
                signature = content.read(32)
            valid = (
                b"ftyp" in signature
                if extension == "mp4"
                else signature.startswith((b"BLENDER", b"\x28\xb5\x2f\xfd", b"\x1f\x8b"))
            )
            if not valid:
                raise ArchiveError("다운로드한 결과가 올바른 MP4 또는 Blender 파일이 아닙니다.")
            # Discovery can race publication (or run after a crash). Preserve
            # a legacy job's original age on disk before making it visible.
            if created is not None:
                os.utime(partial, (created, created))
            partial.replace(target)
            archived = self.track(key, target, created=created)
            return {
                **{k: v for k, v in item.items() if k not in ("download", "archive")},
                "archive": archived,
            }
        except VideoError:
            raise ArchiveError(
                "결과 파일은 공개 HTTPS 저장소에서만 내려받을 수 있습니다."
            ) from None
        except httpx.RequestError:
            raise ArchiveError(
                "생성 결과 저장 연결에 실패했습니다. 파일 보관 완료를 확인하세요."
            ) from None
        finally:
            partial.unlink(missing_ok=True)

    def complete(self, job_id, items, *, completed=None):
        """Start all NEW job outputs' clocks together, after every save succeeds.

        Called only during Service.execute, never on reads or legacy migration.
        """
        completed = time.time() if completed is None else completed
        results = []
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for index, item in enumerate(items):
                key = job_id + "-" + str(index)
                if item.get("archive", {}).get("key") != key:
                    raise ArchiveError("저장 파일 식별자가 일치하지 않습니다.")
                row = db.execute("SELECT path FROM media_assets WHERE key=?", (key,)).fetchone()
                if not row:
                    raise ArchiveError("저장 결과를 확인할 수 없습니다.")
                os.utime(self.safe_path(row[0]), (completed, completed))
                db.execute(
                    "UPDATE media_assets SET saved=?,expires=? WHERE key=?",
                    (completed, None, key),
                )
                results.append(
                    {
                        **item,
                        "archive": {
                            "key": key,
                            "savedAt": completed,
                            "expiresAt": None,
                        },
                    }
                )
        return results

    def discover(self):
        """Register existing result files by mtime, without refreshing their age."""
        patterns = [
            ("archive", re.compile("(" + UUID + r"-\d+)\.(mp4|blend)$"), ""),
            ("recovered", re.compile("(" + UUID + r")\.(mp4|blend)$"), "recovered-"),
        ]
        for folder, pattern, prefix in patterns:
            for path in (self.root / "runtime" / folder).glob("*"):
                match = pattern.fullmatch(path.name)
                if match and path.is_file() and not path.is_symlink():
                    key = prefix + match[1] + ("-" + match[2] if prefix else "")
                    try:
                        self.track(key, path, created=path.stat().st_mtime)
                    except FileNotFoundError:
                        pass  # Another worker may already have removed an expired file.
        for path in self.directory.glob("scene-*.json"):
            match = re.fullmatch(r"scene-(" + UUID + r")\.json", path.name)
            if match and path.is_file() and not path.is_symlink():
                try:
                    self.track("scene-" + match[1], path, created=path.stat().st_mtime)
                except FileNotFoundError:
                    pass
        for folder in (self.root / "runtime/wan").glob("*"):
            if re.fullmatch(UUID, folder.name) and not folder.is_symlink():
                path = folder / "wan.mp4"
                if path.is_file() and not path.is_symlink():
                    try:
                        self.track("wan-" + folder.name, path, created=path.stat().st_mtime)
                    except FileNotFoundError:
                        pass

        for folder in (self.root / "runtime/wan-inputs").glob("*"):
            if re.fullmatch(UUID, folder.name) and not folder.is_symlink():
                path, record = folder / "previs.mp4", folder / "source.json"
                if (
                    path.is_file()
                    and record.is_file()
                    and not path.is_symlink()
                    and not record.is_symlink()
                ):
                    try:
                        self.track("uploaded-" + folder.name, path, created=path.stat().st_mtime)
                    except FileNotFoundError:
                        pass

    def make_permanent(self):
        """Convert retained local files, including expired rows, without deleting bytes."""
        self.discover()
        changed = missing = 0
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for key, relative, expires in db.execute('SELECT key,path,expires FROM media_assets').fetchall():
                path = self.safe_path(relative)
                if not path.is_file():
                    missing += 1
                elif expires is not None:
                    db.execute('UPDATE media_assets SET expires=NULL WHERE key=?', (key,))
                    changed += 1
        return {'filesUpdated':changed, 'missingFiles':missing}

    def cleanup(self, *, now=None):
        self.discover()
        current = time.time() if now is None else now
        removed = 0
        # Only old archive partials are eligible; never walk arbitrary account data.
        for path in self.directory.glob("*.part"):
            if (
                re.fullmatch(r"(?:" + UUID + r"-\d+\.(?:mp4|blend)|scene-" + UUID + r"\.json)\.[a-f0-9]{32}\.part", path.name)
                and not path.is_symlink()
                and path.stat().st_mtime <= current - PARTIAL_RETENTION_SECONDS
            ):
                self.safe_path(path.relative_to(self.root)).unlink()
        return removed
