"""Opt-in, durable usage delivery independent of generation success."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import re
import sqlite3

logger = logging.getLogger('uvicorn.error')
EXCLUDED_USAGE_DATABASES = frozenset({'dev_user', 'aniestkh'})


def usage_database_allowed(database, *, override=False):
    return (isinstance(database, str) and re.fullmatch(r'[A-Za-z0-9_]{1,64}', database) is not None
            and (override is True or database.lower() not in EXCLUDED_USAGE_DATABASES))


def lookup_member_id(context, config):
    """Read the chatbot owner's linked ID without allowing arbitrary SQL targets."""
    database = context.get('db_name')
    chatbot = context.get('chatbotid')
    if not usage_database_allowed(database, override=context.get('usageLogOverride')) or type(chatbot) not in (str, int):
        return None
    chatbot = str(chatbot).strip()
    if not chatbot or len(chatbot) > 36:
        return None
    if not all(config.get(key) for key in
               ('MARIA_DB_HOST', 'MARIA_DB_PORT', 'MARIA_DB_USER', 'MARIA_DB_PASSWORD')):
        return None
    try:
        import pymysql
        connection = pymysql.connect(
            host=config['MARIA_DB_HOST'], port=int(config['MARIA_DB_PORT']),
            user=config['MARIA_DB_USER'], password=config['MARIA_DB_PASSWORD'],
            database=database, charset='utf8mb4', autocommit=True,
            connect_timeout=3, read_timeout=3, write_timeout=3,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT linked_id FROM chatbot_setup WHERE chat_bot_id = %s LIMIT 2',
                               (chatbot,))
                rows = cursor.fetchall()
                if len(rows) != 1 or rows[0][0] is None:
                    return None
                member = str(rows[0][0]).strip()
                return member if member and len(member) <= 50 else None
        finally:
            connection.close()
    except Exception as error:
        logger.warning('[사용량 DB] 회원 조회 불가 · %s · 생성 작업 계속', type(error).__name__)
        return None


def resolve_member_id(context, config, *, allow_lookup=True):
    """Honor a stage's saved member, including a deliberately empty value."""
    if 'usageMemberId' in context:
        return context['usageMemberId']
    return (context.get('mb_id') or (lookup_member_id(context, config) if allow_lookup else None)
            or config.get('AI_USAGE_MB_ID') or None)


def token_count(value):
    return int(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0


def compact_usage(row, context, config):
    """Persist metering and trace IDs only, never nested call payloads or prompts."""
    original = row.get('raw_usage') or {}
    calls = original.get('calls') if isinstance(original, dict) else None
    calls = calls if isinstance(calls, list) else [row]
    raw = {key: token_count(row.get(key)) for key in
           ('input_tokens', 'output_tokens', 'cached_tokens', 'total_tokens')}
    detail_keys = {
        'input_tokens_details': {'cache_write_tokens', 'cached_tokens', 'cache_write_tokens_details'},
        'output_tokens_details': {'reasoning_tokens'},
        'cache_write_tokens_details': {'ttl_30m_tokens', 'ttl_12h_tokens'},
    }
    def add_details(target, source, kind):
        if not isinstance(source, dict):
            return
        for key in detail_keys[kind]:
            value = source.get(key)
            if key == 'cache_write_tokens_details' and isinstance(value, dict):
                add_details(target.setdefault(key, {}), value, key)
            elif type(value) in (int, float) and math.isfinite(value) and value >= 0:
                target[key] = target.get(key, 0) + int(value)
    provider_ids = []
    for call in calls:
        usage = call.get('raw_usage') or {}
        if isinstance(usage, dict):
            for kind in ('input_tokens_details', 'output_tokens_details'):
                add_details(raw.setdefault(kind, {}), usage.get(kind), kind)
        provider_id = call.get('provider_request_id')
        if provider_id and provider_id not in provider_ids:
            provider_ids.append(provider_id)
    raw['input_tokens_details'] = {**raw.get('input_tokens_details', {}), 'cached_tokens': raw.pop('cached_tokens')}
    raw['output_tokens_details'] = raw.get('output_tokens_details', {})
    raw.update(request_id_source='provider' if provider_ids else 'internal',
               internal_request_id=context.get('request_id') or row.get('request_id') or row['id'],
               provider_request_id=provider_ids[-1] if provider_ids else None)
    if len(provider_ids) > 1:
        raw['provider_request_ids'] = provider_ids
    if config.get('OPENAI_ACCOUNT'):
        raw['openai_account'] = config['OPENAI_ACCOUNT']
    pricing = [(call.get('raw_usage') or {}).get('pricing') for call in calls]
    pricing = [item for item in pricing if isinstance(item, dict)]
    if pricing:
        raw['pricing'] = pricing[0] if len(pricing) == 1 else pricing
    raw['call_count'] = len(calls)
    if isinstance(original, dict) and original.get('stage_status'):
        raw['stage_status'] = original['stage_status']
    if isinstance(original, dict) and original.get('partial_fields'):
        raw['partial_fields'] = original['partial_fields']
    # Keep only the fields needed for replay protection and stage linkage.
    raw['_tracking'] = {key: value for key, value in {
        'usage_id': row['id'], 'job_id': context.get('job_id'),
        'source_job_id': context.get('source_job_id'),
        'execution_job_id': context.get('execution_job_id'),
    }.items() if value is not None}
    return raw


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
            table = self.config.get('AI_USAGE_TABLE_NAME', 'AIPRO_API_USAGE_LOG')
            if not usage_database_allowed(database, override=context.get('usageLogOverride')):
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
            raw = compact_usage(row, context, self.config)
            data = {key: row.get(key) for key in (
                'job_id', 'request_id', 'message_id', 'input_tokens', 'output_tokens',
                'cached_tokens', 'total_tokens', 'quantity', 'duration_seconds',
                'cost_usd', 'error_code', 'latency_ms')}
            data.update(chatbot_id=chatbot, session_id=None,
                        job_id=context.get('job_id') or row.get('job_id'),
                        request_id=context.get('request_id') or row.get('request_id') or ident,
                        mb_id=resolve_member_id(context, self.config, allow_lookup=False),
                        job_type=context.get('job_type') or row.get('job_type', 'previs_chat'),
                        provider=row.get('provider', 'unknown'), model=row.get('model', 'unknown'),
                        modality='llm' if row.get('modality', 'text') == 'text' else row['modality'],
                        billing_unit=row.get('billing_unit', 'token'),
                        credits_charged=row.get('credits_charged') or 0,
                        call_status=('success' if row.get('call_status') in
                                     ('completed', 'succeeded', 'success') else 'failed'),
                        raw_usage=None)
            raw.update({key: data[key] for key in ('session_id', 'job_id', 'request_id', 'mb_id', 'job_type')})
            raw['request_id_source'] = 'internal'
            data['raw_usage'] = json.dumps(raw, ensure_ascii=False, allow_nan=False)
            for key in ('input_tokens', 'output_tokens', 'cached_tokens', 'total_tokens'):
                data[key] = token_count(data.get(key))
            # Bounds reflect the existing table; never truncate identifiers silently.
            limits = {'job_id':128, 'session_id':128, 'request_id':128, 'mb_id':50, 'job_type':32,
                      'provider':32, 'model':64, 'modality':16, 'billing_unit':16, 'error_code':64}
            for key, limit in limits.items():
                if data.get(key) is not None and len(str(data[key])) > limit:
                    raise ValueError('column length exceeded: ' + key)
            if row.get('created_at'):
                data['created_at'] = datetime.fromisoformat(row['created_at']).astimezone(
                    timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
            payload = json.dumps({'database':database, 'table':table, 'data':data,
                                  'overrideExcludedDatabase': context.get('usageLogOverride') is True}, allow_nan=False)
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
        excluded = tuple(sorted(EXCLUDED_USAGE_DATABASES))
        placeholders = ','.join('?' for _ in excluded)
        db = self._db()
        try:
            return [{'id': r['id'], '_queue_sequence': r['queue_sequence'], **json.loads(r['data'])} for r in db.execute(
                "SELECT rowid AS queue_sequence,id,data FROM deliveries WHERE delivered=0 AND rowid>? "
                "AND json_type(data,'$.database') = 'text' "
                "AND length(json_extract(data,'$.database')) BETWEEN 1 AND 64 "
                "AND json_extract(data,'$.database') NOT GLOB '*[^A-Za-z0-9_]*' "
                "AND (lower(json_extract(data,'$.database')) NOT IN (" + placeholders + ") "
                "OR json_type(data,'$.overrideExcludedDatabase') = 'true') ORDER BY rowid LIMIT 20",
                (after, *excluded))]
        finally:
            db.close()

    def _insert(self, item):
        if not usage_database_allowed(item.get('database'), override=item.get('overrideExcludedDatabase')):
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
                    # Apply the current table policy to legacy queued deliveries too.
                    data = {**item['data'], 'session_id': None}
                    if data.get('raw_usage'):
                        raw = json.loads(data['raw_usage'])
                        if isinstance(raw, dict):
                            raw['session_id'] = None
                            data['raw_usage'] = json.dumps(raw, ensure_ascii=False, allow_nan=False)
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
