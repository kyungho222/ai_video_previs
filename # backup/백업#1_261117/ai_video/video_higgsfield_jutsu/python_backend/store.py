from datetime import datetime, timezone
import json
import re
import time
from uuid import uuid4
from .memory_db import MemoryDatabase


def scene_brief(messages):
    """Keep recent user instructions, with the latest correction taking priority."""
    selected = []
    for message in reversed(messages):
        candidate = [message, *selected]
        if len("\n\n".join(candidate)) > 7900 and selected:
            break
        selected = candidate
    if len(selected) == 1:
        return selected[0]
    return (
        "Scene instructions in chronological order. Later instructions override earlier instructions.\n\n"
        + "\n\n".join(selected)
    )


def redact(value, *, usage=False):
    if isinstance(value, list):
        return [redact(item, usage=usage) for item in value]
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if usage and key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens",
                                 "reasoning_tokens") and (item is None or type(item) in (int, float)):
                output[key] = item
            elif usage and key in ("raw_usage", "input_tokens_details", "output_tokens_details") and isinstance(item, dict):
                output[key] = redact(item, usage=True)
            elif re.search(r"token|authorization|secret|headers", key, re.I):
                output[key] = "[redacted]"
            elif key == "download":
                output[key] = (
                    {
                        "method": item.get("method"),
                        "expiresAt": item.get("expiresAt"),
                        "url": "[signed URL hidden]",
                    }
                    if isinstance(item, dict)
                    else "[redacted]"
                )
            elif key == "text" and isinstance(item, str):
                try:
                    output[key] = json.dumps(redact(json.loads(item)), ensure_ascii=False)
                except (ValueError, TypeError):
                    output[key] = redact(item)
            else:
                output[key] = redact(item, usage=usage)
        return output
    if isinstance(value, str):
        value = re.sub(r'(?i)(bearer\s+)[^\s"\x27]+', r"\1[redacted]", value)
        value = re.sub(r'https://[^\s"<>]+\?[^\s"<>]+', "[signed URL hidden]", value)
    return value


class JobStore(MemoryDatabase):
    def __init__(self, path=None):
        """Accept legacy path callers; job state remains process-local memory only."""
        super().__init__()
        self._execution_tasks = {}
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, history TEXT, active TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, session TEXT, state TEXT, data TEXT, lease REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events(job TEXT, sequence INTEGER, value TEXT, PRIMARY KEY(job,sequence))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS scene_versions(scene TEXT, version INTEGER, session TEXT, job TEXT UNIQUE, PRIMARY KEY(scene,version))"
            )

    def track_execution(self, job_id, task):
        if self._execution_tasks.get(job_id) is task:
            return
        self._execution_tasks[job_id] = task

        def release(completed):
            if self._execution_tasks.get(job_id) is completed:
                self._execution_tasks.pop(job_id, None)

        task.add_done_callback(release)

    def _emit(self, db, job_id, kind, value):
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),-1)+1 FROM events WHERE job=?", (job_id,)
        ).fetchone()[0]
        event = {
            "sequence": sequence,
            "type": kind,
            "value": redact(value, usage=kind == "ai_usage"),
            "time": datetime.now(timezone.utc).isoformat(),
        }
        db.execute(
            "INSERT INTO events VALUES (?,?,?)",
            (job_id, sequence, json.dumps(event, ensure_ascii=False)),
        )

    def _expire(self, db):
        expired = db.execute(
            "SELECT id,session FROM jobs WHERE state=? AND lease<?", ("running", time.time())
        ).fetchall()
        for job_id, session_id in expired:
            execution = self._execution_tasks.get(job_id)
            if execution is not None and not execution.done():
                db.execute("UPDATE jobs SET lease=? WHERE id=?", (time.time() + 90, job_id))
                self._emit(
                    db,
                    job_id,
                    "log",
                    "[previs-recovery] 생존 신호 갱신이 지연됐지만 실행 중인 작업을 확인했습니다. 생성을 계속합니다.",
                )
                continue
            db.execute("UPDATE jobs SET state=? WHERE id=?", ("failed", job_id))
            db.execute(
                "UPDATE sessions SET active=NULL WHERE id=? AND active=?", (session_id, job_id)
            )
            self._emit(
                db,
                job_id,
                "error",
                "작업 생존 신호와 실행 중인 작업을 확인할 수 없습니다. 마지막 operationId의 상태를 확인하세요. 원격 편집은 중복 실행하지 않았습니다.",
            )

    def prompt_context(self, session_id, *, new_scene=False):
        """Read the completed baseline that a new request would inherit."""
        with self.db() as db:
            row = db.execute('SELECT active FROM sessions WHERE id=?', (session_id,)).fetchone()
            if row and row[0]:
                raise ValueError('현재 요청이 완료된 후 보내세요.')
            if new_scene or not row:
                return ''
            rows = db.execute(
                "SELECT j.data FROM scene_versions v JOIN jobs j ON j.id=v.job "
                "WHERE v.scene=(SELECT scene FROM scene_versions WHERE session=? ORDER BY rowid DESC LIMIT 1) "
                "AND j.state='completed' ORDER BY v.version DESC", (session_id,),
            ).fetchall()
            for row in rows:
                value = json.loads(row[0])
                if value.get('codeOperations') and any(item.get('mediaType') == 'video/mp4' for item in value.get('media', [])):
                    return value.get('scenePrompt', '')
        return ''

    def create(self, message, session_id=None, duration=5, aspect="16:9", new_scene=False, *,
               scenario_titles=None, work_settings=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0] >= 64:
                raise ValueError(
                    "대기 중인 작업이 많습니다. 진행 중인 작업이 완료된 후 요청하세요."
                )
            row = db.execute(
                "SELECT history,active FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row and row[1]:
                raise ValueError("현재 요청이 완료된 후 보내세요.")
            if not row:
                session_id = str(uuid4())
                history = []
                db.execute("INSERT INTO sessions VALUES (?,?,NULL)", (session_id, "[]"))
            else:
                history = json.loads(row[0])
            previous = (
                None
                if new_scene
                else db.execute(
                    "SELECT scene FROM scene_versions WHERE session=? ORDER BY rowid DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            )
            scene_id = previous[0] if previous else str(uuid4())
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM scene_versions WHERE scene=?", (scene_id,)
            ).fetchone()[0]
            completed = db.execute(
                "SELECT j.data,v.version FROM scene_versions v JOIN jobs j ON j.id=v.job WHERE v.scene=? AND j.state='completed' ORDER BY v.version DESC",
                (scene_id,),
            ).fetchall()
            base = next(
                (
                    row
                    for row in completed
                    if json.loads(row[0]).get("codeOperations")
                    and any(
                        item.get("mediaType") == "video/mp4"
                        for item in json.loads(row[0]).get("media", [])
                    )
                ),
                None,
            )
            base_snapshot = None
            if base:
                data = json.loads(base[0])
                base_snapshot = {
                    key: data.get(key)
                    for key in (
                        "id",
                        "scenePrompt",
                        "plan",
                        "workerDrafts",
                        "codeOperations",
                        "remoteProjects",
                        "duration",
                        "aspect",
                        "cameraScale",
                        "cameraMetrics",
                    )
                }
                if data.get("scenarioTitles"):
                    base_snapshot["scenarioTitles"] = data["scenarioTitles"]
                base_snapshot["version"] = base[1]
                history = [{"role": "user", "content": data.get("scenePrompt", "")}]
            else:
                history = []
            history.append({"role": "user", "content": message})
            job = {
                "id": str(uuid4()),
                "duration": duration,
                "aspect": aspect,
                "media": [],
                "mutated": False,
                "sceneId": scene_id,
                "version": version,
                "baseVersion": base[1] if base else None,
                "request": message,
                "baseSnapshot": base_snapshot,
                "codeOperations": [],
                "remoteProjects": [],
                "scenePrompt": scene_brief(
                    [item["content"] for item in history[-20:] if item["role"] == "user"]
                ),
            }
            titles = scenario_titles or (base_snapshot or {}).get("scenarioTitles")
            if titles:
                job["scenarioTitles"] = titles
            if work_settings:
                job["workSettings"] = work_settings
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?)",
                (job["id"], session_id, "running", json.dumps(job), time.time() + 90),
            )
            db.execute(
                "INSERT INTO scene_versions VALUES (?,?,?,?)",
                (scene_id, version, session_id, job["id"]),
            )
            db.execute(
                "UPDATE sessions SET history=?,active=? WHERE id=?",
                (json.dumps(history[-40:], ensure_ascii=False), job["id"], session_id),
            )
            self._emit(
                db,
                job["id"],
                "log",
                f"v{version} 프리비즈 "
                + (f"수정 중 · 기준 v{base[1]}" if base else "새로 생성 중"),
            )
            return job, session_id, history[-20:]

    def checkpoint(self, job):
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET data=? WHERE id=? AND state='running'",
                (json.dumps(job, ensure_ascii=False), job["id"]),
            )

    def versions(self, session_id):
        with self.db() as db:
            self._expire(db)
            rows = db.execute(
                "SELECT v.scene,v.version,j.id,j.state,j.data FROM scene_versions v JOIN jobs j ON j.id=v.job WHERE v.session=? ORDER BY v.rowid",
                (session_id,),
            ).fetchall()
        return [
            {
                "sceneId": scene,
                "version": version,
                "jobId": job,
                "state": state,
                "baseVersion": json.loads(data).get("baseVersion"),
                "request": json.loads(data).get("request", ""),
            }
            for scene, version, job, state, data in rows
        ]

    def emit(self, job_id, kind, value):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._emit(db, job_id, kind, value)

    def heartbeat(self, job_id):
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET lease=? WHERE id=? AND state=?",
                (time.time() + 90, job_id, "running"),
            )

    def finish(self, job, output=None, error=None):
        if error is not None:
            error = (
                str(error).strip()
                or "작업 처리 중 오류가 발생했습니다. 상세 오류 메시지가 비어 있습니다."
            )
        elif not isinstance(output, dict) or not isinstance(output.get("message"), str):
            error = "작업 결과가 반환되지 않았습니다. 완료로 처리하지 않았습니다."
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session,state FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if not row or row[1] != "running":
                return
            db.execute(
                "UPDATE jobs SET state=?,data=? WHERE id=?",
                (
                    "failed" if error is not None else "completed",
                    json.dumps(job, ensure_ascii=False),
                    job["id"],
                ),
            )
            db.execute(
                "UPDATE sessions SET active=NULL WHERE id=? AND active=?", (row[0], job["id"])
            )
            if error is not None:
                self._emit(db, job["id"], "error", error)
            else:
                history = json.loads(
                    db.execute("SELECT history FROM sessions WHERE id=?", (row[0],)).fetchone()[0]
                )
                history.append({"role": "assistant", "content": output["message"]})
                db.execute(
                    "UPDATE sessions SET history=? WHERE id=?",
                    (json.dumps(history[-40:], ensure_ascii=False), row[0]),
                )
                self._emit(db, job["id"], "complete", output)

    def get(self, job_id, after=-1):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            row = db.execute("SELECT state,data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            events = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT value FROM events WHERE job=? AND sequence>? ORDER BY sequence",
                    (job_id, after),
                )
            ]
            return {"state": row[0], "events": events, "data": json.loads(row[1])}

    def replace_media(self, job_id, index, item):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM jobs WHERE id=? AND state='completed'", (job_id,)
            ).fetchone()
            if row:
                data = json.loads(row[0])
                data["media"][index] = item
                db.execute(
                    "UPDATE jobs SET data=? WHERE id=?",
                    (json.dumps(data, ensure_ascii=False), job_id),
                )

    def scene_prompt(self, job_id):
        """Use the job snapshot; recover legacy history only with exact alignment."""
        with self.db() as db:
            row = db.execute("SELECT session,data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return ""
            data = json.loads(row[1])
            if isinstance(data.get("scenePrompt"), str):
                return data["scenePrompt"]
            session = db.execute("SELECT history FROM sessions WHERE id=?", (row[0],)).fetchone()
            if not session:
                return ""
            users = [
                item["content"] for item in json.loads(session[0]) if item.get("role") == "user"
            ]
            ids = [
                r[0]
                for r in db.execute("SELECT id FROM jobs WHERE session=? ORDER BY rowid", (row[0],))
            ]
            # Truncated histories cannot reliably be associated with older jobs.
            return scene_brief(users[: ids.index(job_id) + 1]) if len(users) == len(ids) else ""

    def history_snapshot(self):
        with self.db() as db:
            jobs = [(identifier, state, json.loads(data))
                    for identifier, state, data in db.execute('SELECT id,state,data FROM jobs')]
            events = [(identifier, json.loads(raw)['value']) for identifier, raw in db.execute(
                "SELECT job,value FROM events WHERE json_extract(value,'$.type')='ai_usage'")]
        return jobs, events

    def completed_sources(self, limit=20):
        with self.db() as db:
            return [(identifier,json.loads(data)) for identifier,data in db.execute(
                "SELECT id,data FROM jobs WHERE state='completed' ORDER BY rowid DESC LIMIT ?", (limit,))]

    def refresh_media(self, job_id, index, **fields):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT data FROM jobs WHERE id=? AND state='completed'",(job_id,)).fetchone()
            if row:
                data=json.loads(row[0])
                data['media'][index].update(fields)
                db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(data),job_id))
