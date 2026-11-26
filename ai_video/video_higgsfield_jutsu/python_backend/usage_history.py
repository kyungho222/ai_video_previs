"""Durable, read-only console history grouped by the source previs job."""

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
from threading import Lock

from .ai_usage import COLUMNS
from .retention_policy import is_expired
from .repositories import runtime_base
from .store import redact

UUID = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'


class UsageHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._import_lock = Lock()
        self._legacy_imported = False
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS history_jobs(job_id TEXT PRIMARY KEY,data TEXT,created REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS history_usage(id TEXT PRIMARY KEY,job_id TEXT,data TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS history_usage_job ON history_usage(job_id)')
            db.execute('CREATE TABLE IF NOT EXISTS history_deleted(job_id TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS history_hidden(job_id TEXT PRIMARY KEY)')
            db.execute('CREATE INDEX IF NOT EXISTS history_created ON history_jobs(created DESC,job_id)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_previs(self, job, state, *, created=None, kind='job', only_if_missing=False):
        identifier = job.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(UUID, identifier):
            return
        fields = ('request', 'scenePrompt', 'chatModel', 'sessionId', 'version', 'promptFilter', 'plan', 'workSettings', 'usageMemberId', 'usageJobId', 'usageDbName', 'usageLogEnabled', 'usageLogOverride', 'requestId', 'directorAttempts', 'unresolvedValidation', 'intentQuality')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM history_deleted WHERE job_id=?',(identifier,)).fetchone():
                return
            previous = db.execute('SELECT data FROM history_jobs WHERE job_id=?', (identifier,)).fetchone()
            if previous and only_if_missing:
                return
            record = json.loads(previous['data']) if previous else {'jobId':identifier, 'kind':kind}
            record.update({key:job[key] for key in fields if key in job})
            record['state'] = state
            db.execute('INSERT INTO history_jobs VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET data=excluded.data',
                       (identifier,json.dumps(redact(record),ensure_ascii=False),created or time.time()))

    def latest_completed_session_job(self, session_id):
        """Recover revision provenance after process-local state is lost."""
        with self.db() as db:
            row=db.execute("SELECT job_id FROM history_jobs WHERE json_extract(data,'$.sessionId')=? "
                           "AND json_extract(data,'$.state')='completed' ORDER BY created DESC LIMIT 1",
                           (session_id,)).fetchone()
        return row['job_id'] if row else None

    def record_usage(self, job_id, row):
        if not isinstance(row, dict) or not isinstance(row.get('id'),str):
            return
        value = redact({key:row.get(key) for key in COLUMNS},usage=True)
        value['job_id'] = job_id
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM history_deleted WHERE job_id=?',(job_id,)).fetchone():
                return
            db.execute('INSERT INTO history_usage VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (row['id'],job_id,json.dumps(value,ensure_ascii=False)))

    def delete(self, job_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO history_deleted VALUES(?)',(job_id,))
            db.execute('DELETE FROM history_usage WHERE job_id=?',(job_id,))
            db.execute('DELETE FROM history_jobs WHERE job_id=?',(job_id,))
            db.execute('DELETE FROM history_hidden WHERE job_id=?',(job_id,))

    def set_note(self, job_id, note):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM history_jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is None:
                return False
            record = json.loads(row['data'])
            record['note'] = note
            db.execute('UPDATE history_jobs SET data=? WHERE job_id=?',
                       (json.dumps(record, ensure_ascii=False), job_id))
        return True

    def set_hidden(self, job_id, hidden):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM history_jobs WHERE job_id=?',(job_id,)).fetchone():
                return False
            if hidden:
                db.execute('INSERT OR IGNORE INTO history_hidden VALUES(?)',(job_id,))
            else:
                db.execute('DELETE FROM history_hidden WHERE job_id=?',(job_id,))
        return True

    def get(self, job_id):
        with self.db() as db:
            row = db.execute('SELECT data,created FROM history_jobs WHERE job_id=?',(job_id,)).fetchone()
        return {**json.loads(row['data']),'createdAt':row['created']} if row else None

    def usage(self, job_id):
        with self.db() as db:
            rows = db.execute('SELECT data FROM history_usage WHERE job_id=? ORDER BY rowid',(job_id,)).fetchall()
        return [json.loads(row['data']) for row in rows]

    def sync(self, service, manager):
        with self._import_lock:
            if not self._legacy_imported:
                self._import_legacy(service, manager)
                self._legacy_imported = True

    def _import_legacy(self, service, manager):
        # Existing archive snapshots recover old prompts even after process-local chats disappear.
        storage_root = runtime_base(service.root)
        for directory, kind, pattern in ((storage_root/'runtime/archive','job','scene-*.json'),
                                         (storage_root/'runtime/recovered','recovered','*.json')):
            for path in directory.glob(pattern):
                identifier = path.stem.removeprefix('scene-')
                if path.is_symlink() or not re.fullmatch(UUID,identifier):
                    continue
                try:
                    data = json.loads(path.read_text(encoding='utf-8-sig'))
                    self.save_previs({'id':identifier,**{k:data[k] for k in ('request','scenePrompt','plan') if k in data}},
                                     'completed',created=path.stat().st_mtime,kind=kind,only_if_missing=True)
                except (ValueError,OSError,TypeError):
                    continue
        jobs, events = service.store.history_snapshot()
        for identifier,state,data in jobs:
            self.save_previs({**data,'id':identifier},state)
        for identifier,value in events:
            self.record_usage(identifier,value)
        videos = manager.store.all_jobs()
        for video in videos:
            self.record_video_source(video)

    def record_video_source(self, video):
        source = video['source']
        kind = 'uploaded' if source.get('uploadedId') else 'recovered' if source.get('recoveredId') else 'job'
        self.save_previs({'id':self.source_id(video),'scenePrompt':video['options'].get('scenePrompt','')},
                         'archived',created=video['created'],kind=kind,only_if_missing=True)

    def media(self, record, service, manager):
        identifier = record['jobId']
        result = []
        if record.get('kind') == 'uploaded':
            try:
                value,path = manager.previs.get(identifier)
                if path.is_file() and not is_expired(value['expiresAt']):
                    result.append({'url':value['videoUrl'],'expiresAt':value['expiresAt']})
            except (ValueError,OSError,RuntimeError,AttributeError,KeyError):
                pass
            return result
        directory = runtime_base(service.root) / ('runtime/recovered' if record.get('kind')=='recovered' else 'runtime/archive')
        pattern = identifier+'.mp4' if record.get('kind')=='recovered' else identifier+'-*.mp4'
        for path in directory.glob(pattern):
            match = re.fullmatch(identifier+r'-(\d+)\.mp4',path.name)
            if record.get('kind')!='recovered' and not match:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            index = int(match[1]) if match else 0
            key = 'recovered-'+identifier+'-mp4' if record.get('kind')=='recovered' else f'{identifier}-{index}'
            asset = service.archive.record(key)
            try:
                expires = asset['expiresAt'] if asset else None
            except FileNotFoundError:
                continue
            if not is_expired(expires):
                url = '/api/recovered/'+path.name if record.get('kind')=='recovered' else f'/api/jobs/{identifier}/media/{index}'
                result.append({'url':url,'expiresAt':expires,'index':index})
        return result

    def video_summary(self, video, service):
        path = runtime_base(service.root)/'runtime/wan'/video['id']/'wan.mp4'
        asset = service.archive.record('wan-' + video['id'])
        expires = asset['expiresAt'] if asset else None
        available = video['state']=='completed' and path.is_file() and not path.is_symlink() and not is_expired(expires)
        return {'id':video['id'],'state':video['state'],'createdAt':video['created'],
                'videoUrl':'/api/video/jobs/'+video['id']+'/video' if available else None,
                'expiresAt':expires}

    @staticmethod
    def source_id(video):
        source = video['source']
        return source.get('jobId') or source.get('uploadedId') or source.get('recoveredId')

    def list_records(self, service, manager, *, offset=0, limit=20, hidden=False):
        self.sync(service,manager)
        with self.db() as db:
            rows = db.execute('SELECT job_id,data,created FROM history_jobs WHERE '
                              'EXISTS(SELECT 1 FROM history_hidden h WHERE h.job_id=history_jobs.job_id)=? '
                              'ORDER BY created DESC,job_id LIMIT ? OFFSET ?',
                              (int(hidden),limit+1,offset)).fetchall()
        videos = manager.store.for_sources([row['job_id'] for row in rows[:limit]])
        items = []
        for row in rows[:limit]:
            record = {**json.loads(row['data']), 'createdAt':row['created']}
            record['hidden'] = hidden
            record.pop('promptFilter',None)
            plan = record.pop('plan',None)
            record['appliedPrompt'] = plan.get('prompt_markdown') if isinstance(plan,dict) else None
            record['previs'] = self.media(record,service,manager)
            record['videos'] = [self.video_summary(v,service) for v in videos if self.source_id(v)==record['jobId'] and v['state']=='completed']
            items.append(record)
        return {'items':items,'nextOffset':offset+limit if len(rows)>limit else None}

    def details(self, job_id, service, manager):
        self.sync(service,manager)
        record = self.get(job_id)
        if record is None:
            return None
        usage = self.usage(job_id)
        associated = []
        for video in manager.store.for_source(job_id):
            rows = [{key:row.get(key) for key in COLUMNS} for row in video['metadata'].get('ai_usage',[])]
            rows = redact(rows,usage=True)
            usage.extend(rows)
            associated.append({'job_id':video['id'],'state':video['state'],'source':video['source'],
                               'prompt':video['options'].get('prompt'),
                               'submitted_prompt':video['metadata'].get('submittedPrompt'),'ai_usage':rows})
        return redact({'job_id':job_id,'columns':list(COLUMNS),'ai_usage':usage,
                       'previs':record,'videos':associated},usage=True)
