"""Local SQLite video jobs and character voice registry."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4



class VideoJobs:

    def __init__(self, path, *, label="영상모델", completion_message=None):
        self.label = label
        self.completion_message = (
            completion_message or f"{label} 영상이 생성되었습니다. 보관기간은 5일입니다."
        )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS wan_jobs(id TEXT PRIMARY KEY, source TEXT, options TEXT, state TEXT, provider_id TEXT, message TEXT, lease REAL, created REAL, metadata TEXT)"
            )
            if "owner" not in {row[1] for row in db.execute("PRAGMA table_info(wan_jobs)")}:
                db.execute("ALTER TABLE wan_jobs ADD COLUMN owner TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS wan_active_source ON wan_jobs(source) WHERE state IN ('queued','preparing','submitting','running','downloading')"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS character_registry(character_id TEXT PRIMARY KEY, identity_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS wan_source_id ON wan_jobs("
                       "COALESCE(json_extract(source,'$.jobId'),json_extract(source,'$.uploadedId'),"
                       "json_extract(source,'$.recoveredId')))")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register_characters(self, records):
        resolved = []
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for record in records:
                row = db.execute("SELECT data FROM character_registry WHERE character_id=?",
                                 (record["character_id"],)).fetchone()
                value = dict(record)
                if row:
                    stored = json.loads(row["data"])
                    if stored["identity_key"] != record["identity_key"]:
                        raise ValueError("Character ID conflicts with an existing asset identity")
                    value.update(voice_language=stored["voice_language"],
                                 voice_description=stored["voice_description"])
                db.execute(
                    "INSERT INTO character_registry(character_id,identity_key,data) VALUES(?,?,?) "
                    "ON CONFLICT(character_id) DO UPDATE SET data=excluded.data",
                    (value["character_id"], value["identity_key"], json.dumps(value, ensure_ascii=False)),
                )
                resolved.append(value)
        return resolved

    def save_character_voices(self, profiles):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for profile in profiles:
                row = db.execute("SELECT data FROM character_registry WHERE character_id=?",
                                 (profile["character_id"],)).fetchone()
                if row is None:
                    raise ValueError("Unknown voice owner: " + profile["character_id"])
                record = json.loads(row["data"])
                if record["voice_description"] and (
                    record["voice_description"] != profile["voice_description"]
                    or record["voice_language"] != profile["voice_language"]
                ):
                    raise ValueError("Reuse the stored voice setting for " + profile["character_id"])
                if profile["voice_language"] == "none":
                    continue
                record.update(voice_language=profile["voice_language"], voice_description=profile["voice_description"])
                db.execute("UPDATE character_registry SET data=? WHERE character_id=?",
                           (json.dumps(record, ensure_ascii=False), record["character_id"]))

    def decode(self, row):
        if row is None:
            return None
        data = dict(row)
        for key in ("source", "options", "metadata"):
            data[key] = json.loads(data[key])
        return data

    def create(self, request_id, source, options, *, initial_metadata=None):
        source_json = json.dumps(source, sort_keys=True)
        options_json = json.dumps(options, sort_keys=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM wan_jobs WHERE id=?", (request_id,)).fetchone()
            if row:
                if row["source"] != source_json or row["options"] != options_json:
                    raise ValueError(
                        f"이미 사용한 요청 ID입니다. 기존 {self.label} 작업 상태를 확인하세요."
                    )
                return (self.decode(row), False)
            if (
                db.execute(
                    "SELECT COUNT(*) FROM wan_jobs WHERE state IN ('queued','preparing','submitting','running','downloading')"
                ).fetchone()[0]
                >= 64
            ):
                raise ValueError(
                    f"{self.label} 대기 작업이 많습니다. 기존 작업이 완료된 후 요청하세요."
                )
            try:
                db.execute(
                    "INSERT INTO wan_jobs(id,source,options,state,provider_id,message,lease,created,metadata) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        request_id,
                        source_json,
                        options_json,
                        "queued",
                        None,
                        f"{self.label} 생성 준비 중입니다.",
                        0,
                        time.time(),
                        json.dumps(initial_metadata or {}, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    f"이 프리비즈의 {self.label} 생성이 이미 진행 중입니다. 기존 작업 상태를 확인하세요."
                ) from None
            return (
                self.decode(
                    db.execute("SELECT * FROM wan_jobs WHERE id=?", (request_id,)).fetchone()
                ),
                True,
            )

    def get(self, job_id):
        with self.db() as db:
            return self.decode(
                db.execute("SELECT * FROM wan_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def recent(self):
        with self.db() as db:
            return [
                self.decode(row)
                for row in db.execute("SELECT * FROM wan_jobs ORDER BY created DESC LIMIT 30")
            ]

    def unfinished(self):
        with self.db() as db:
            return [
                self.decode(row)
                for row in db.execute(
                    "SELECT * FROM wan_jobs WHERE state NOT IN ('completed','failed') ORDER BY created"
                )
            ]

    def all_jobs(self):
        with self.db() as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM wan_jobs ORDER BY created DESC')]

    def for_source(self, source_id):
        return self.for_sources([source_id])

    def for_sources(self, source_ids):
        if not source_ids:
            return []
        placeholders = ','.join('?' for _ in source_ids)
        with self.db() as db:
            return [self.decode(row) for row in db.execute(
                "SELECT * FROM wan_jobs WHERE COALESCE(json_extract(source,'$.jobId'),"
                "json_extract(source,'$.uploadedId'),json_extract(source,'$.recoveredId')) IN (" +
                placeholders + ") ORDER BY created DESC", source_ids)]

    def claim(self, job_id, owner=None):
        with self.db() as db:
            return bool(
                db.execute(
                    "UPDATE wan_jobs SET lease=?,owner=? WHERE id=? AND lease<? AND state NOT IN ('completed','failed')",
                    (time.time() + 90, owner or str(uuid4()), job_id, time.time()),
                ).rowcount
            )

    def begin_submission(self, job_id, owner):
        with self.db() as db:
            return bool(
                db.execute(
                    "UPDATE wan_jobs SET state='submitting',message=? WHERE id=? AND owner=? AND lease>? AND state='preparing' AND provider_id IS NULL",
                    (
                        f"{self.label}에 프리비즈 MP4와 영상 지시를 전달합니다.",
                        job_id,
                        owner,
                        time.time(),
                    ),
                ).rowcount
            )

    def publish_result(self, job_id, owner, candidate, target):
        from .media_archive import RETENTION_SECONDS

        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT metadata FROM wan_jobs WHERE id=? AND owner=? AND lease>? AND state='downloading'",
                (job_id, owner, time.time()),
            ).fetchone()
            if not row:
                return False
            candidate.replace(target)
            completed = time.time()
            metadata = {
                **json.loads(row[0]),
                "completedAt": completed,
                "expiresAt": completed + RETENTION_SECONDS,
            }
            db.execute(
                "UPDATE wan_jobs SET state='completed',message=?,lease=0,metadata=? WHERE id=?",
                (self.completion_message, json.dumps(metadata), job_id),
            )
            return True

    def update(self, job_id, *, owner=None, **values):
        allowed = {"state", "provider_id", "message", "metadata", "lease"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid video job fields")
        if "metadata" in values:
            values["metadata"] = json.dumps(values["metadata"])
        with self.db() as db:
            condition = " AND owner=? AND lease>?" if owner is not None else ""
            arguments = (
                (*values.values(), job_id, owner, time.time())
                if owner is not None
                else (*values.values(), job_id)
            )
            return bool(
                db.execute(
                    "UPDATE wan_jobs SET "
                    + ",".join((k + "=?" for k in values))
                    + " WHERE id=?"
                    + condition,
                    arguments,
                ).rowcount
            )

    def heartbeat(self, job_id, owner=None):
        return self.update(job_id, owner=owner, lease=time.time() + 90)

