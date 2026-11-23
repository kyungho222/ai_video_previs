"""Opt-in, durable usage delivery independent of generation success."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import sqlite3

logger = logging.getLogger('uvicorn.error')


class UsageDatabase:
    def __init__(self, config, path):
        self.config = config
        self.path = path
        self._flush_lock = asyncio.Lock()
        self._delivery_cursor = 0

    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS deliveries '
                   '(id TEXT PRIMARY KEY, data TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)')
        return db

    def submit(self, row, context):
        if context.get('usageLogEnabled') is not True:
            return
        try:
            database = context.get('db_name')
            allowed = {'dev_user', 'aniestkh'}
            table = self.config.get('AI_USAGE_TABLE_NAME', 'AIPRO_API_USAGE_LOG')
            if not database or database not in allowed or not re.fullmatch(r'[A-Za-z0-9_]+', database):
                raise ValueError('unapproved database')
            if not re.fullmatch(r'[A-Za-z0-9_]+', table):
                raise ValueError('invalid table')
            for key in ('MARIA_DB_HOST', 'MARIA_DB_PORT', 'MARIA_DB_USER', 'MARIA_DB_PASSWORD'):
                if not self.config.get(key):
                    raise ValueError('missing connection setting')
            chatbot = str(context.get('chatbotid') or row.get('chatbot_id') or '')
            if not chatbot or len(chatbot) > 36:
                raise ValueError('chatbotid required, max 36 characters')
            ident = row['id']
            session = context.get('session_id') or row.get('session_id')
            numeric_session = (int(session) if str(session).isdigit()
                               and 0 <= int(session) <= 18446744073709551615 else None)
            raw = dict(row.get('raw_usage') or {})
            raw['_tracking'] = {**context, 'usage_id': ident, 'session_id': session,
                                'provider_request_id': row.get('provider_request_id')}
            data = {key: row.get(key) for key in (
                'job_id', 'request_id', 'message_id', 'input_tokens', 'output_tokens',
                'cached_tokens', 'total_tokens', 'quantity', 'duration_seconds',
                'cost_usd', 'error_code', 'latency_ms')}
            data.update(chatbot_id=chatbot, session_id=numeric_session,
                        job_id=context.get('job_id') or row.get('job_id'),
                        request_id=context.get('request_id') or row.get('request_id') or ident,
                        mb_id=context.get('mb_id') or self.config.get('AI_USAGE_MB_ID', 'shortform'),
                        job_type=context.get('job_type') or row.get('job_type', 'previs_chat'),
                        provider=row.get('provider', 'unknown'), model=row.get('model', 'unknown'),
                        modality='llm' if row.get('modality', 'text') == 'text' else row['modality'],
                        billing_unit=row.get('billing_unit', 'token'),
                        credits_charged=row.get('credits_charged') or 0,
                        call_status=('success' if row.get('call_status') in
                                     ('completed', 'succeeded', 'success') else 'failed'),
                        raw_usage=json.dumps(raw, ensure_ascii=False, allow_nan=False))
            # Bounds reflect the existing table; never truncate identifiers silently.
            limits = {'job_id':128, 'request_id':128, 'mb_id':50, 'job_type':32,
                      'provider':32, 'model':64, 'modality':16, 'billing_unit':16, 'error_code':64}
            for key, limit in limits.items():
                if data.get(key) is not None and len(str(data[key])) > limit:
                    raise ValueError('column length exceeded: ' + key)
            if row.get('created_at'):
                data['created_at'] = datetime.fromisoformat(row['created_at']).astimezone(
                    timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
            payload = json.dumps({'database':database, 'table':table, 'data':data}, allow_nan=False)
            db = self._db()
            try:
                with db:
                    db.execute('INSERT OR IGNORE INTO deliveries(id,data) VALUES(?,?)',
                               (ident, payload))
            finally:
                db.close()
        except Exception as error:
            logger.warning('[사용량 DB] 등록 대기 기록 실패 · job=%s · %s',
                           context.get('job_id'), type(error).__name__)

    def pending(self, *, after=0):
        if not self.path.exists():
            return []
        db = self._db()
        try:
            return [{'id': r['id'], '_queue_sequence': r['queue_sequence'], **json.loads(r['data'])} for r in db.execute(
                "SELECT rowid AS queue_sequence,id,data FROM deliveries WHERE delivered=0 AND rowid>? "
                "AND json_extract(data,'$.database') IN ('dev_user','aniestkh') ORDER BY rowid LIMIT 20", (after,))]
        finally:
            db.close()

    def _insert(self, item):
        if item.get('database') not in {'dev_user', 'aniestkh'}:
            raise ValueError('unapproved database')
        import pymysql
        conn = pymysql.connect(host=self.config['MARIA_DB_HOST'],
                               port=int(self.config['MARIA_DB_PORT']),
                               user=self.config['MARIA_DB_USER'], password=self.config['MARIA_DB_PASSWORD'],
                               database=item['database'], charset='utf8mb4',
                               connect_timeout=3, read_timeout=5, write_timeout=5)
        lock = 'usage-' + hashlib.sha256((item['database'] + ':' + item['id']).encode()).hexdigest()[:48]
        try:
            with conn.cursor() as cur:
                cur.execute("SET time_zone = '+00:00'")
                cur.execute('SELECT GET_LOCK(%s, 2)', (lock,))
                if cur.fetchone()[0] != 1:
                    raise RuntimeError('usage lock unavailable')
                try:
                    table = '`' + item['table'] + '`'
                    data = item['data']
                    # Server-side receipt also handles a lost acknowledgement after COMMIT.
                    cur.execute('SELECT id FROM ' + table + ' WHERE request_id=%s AND '
                                "CASE WHEN JSON_VALID(raw_usage) THEN JSON_UNQUOTE(JSON_EXTRACT(raw_usage, '$._tracking.usage_id')) END=%s LIMIT 1",
                                (data['request_id'], item['id']))
                    if cur.fetchone() is None:
                        columns = ','.join('`'+key+'`' for key in data)
                        cur.execute('INSERT INTO '+table+' ('+columns+') VALUES ('+
                                    ','.join(['%s']*len(data))+')', tuple(data.values()))
                        conn.commit()
                finally:
                    cur.execute('SELECT RELEASE_LOCK(%s)', (lock,))
        finally:
            conn.close()

    async def flush(self):
        async with self._flush_lock:
            try:
                items = self.pending(after=self._delivery_cursor)
                if not items and self._delivery_cursor:
                    self._delivery_cursor = 0
                    items = self.pending()
                for item in items:
                    self._delivery_cursor = item['_queue_sequence']
                    try:
                        await asyncio.to_thread(self._insert, item)
                    except Exception as error:
                        logger.warning('[사용량 DB] 등록 실패 · 재시도 대기 · job=%s · %s',
                                       item['data'].get('job_id'), type(error).__name__)
                        continue
                    db = self._db()
                    try:
                        with db:
                            db.execute('UPDATE deliveries SET delivered=1 WHERE id=?', (item['id'],))
                    finally:
                        db.close()
                    logger.info('[사용량 DB] 등록 완료 · job=%s', item['data'].get('job_id'))
            except Exception as error:
                logger.warning('[사용량 DB] 대기열 점검 실패 · %s', type(error).__name__)

    async def run(self):
        while True:
            await self.flush()
            await asyncio.sleep(5)
