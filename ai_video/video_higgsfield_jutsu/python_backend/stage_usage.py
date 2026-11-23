"""One database usage receipt per stage, provider, model and metering unit."""

from datetime import datetime, timezone
from contextvars import ContextVar
import json
import logging
import math
from uuid import NAMESPACE_URL, uuid5

from .ai_usage import COLUMNS
from .store import redact

SUCCESS = {'completed', 'succeeded', 'success'}
METRICS = ('input_tokens', 'output_tokens', 'cached_tokens', 'total_tokens', 'quantity',
           'duration_seconds', 'credits_charged', 'cost_usd', 'latency_ms')
STAGE_USAGE_SINK = ContextVar('stage_usage_sink', default=None)


def aggregate_stage_usage(rows, context, *, state):
    """Never mix different providers, models, modalities, billing units or jobs."""
    identity = context.get('job_id') or context.get('request_id')
    if not identity:
        raise ValueError('stage job_id or request_id required')
    stage = context['job_type']
    unique = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = redact({key: row[key] for key in COLUMNS if key in row}, usage=True)
        signature = tuple(str(value.get(key) or default) for key, default in (
            ('provider', 'unknown'), ('model', 'unknown'), ('modality', 'text'), ('billing_unit', 'token')))
        key = (signature, value.get('id') or json.dumps(value, sort_keys=True))
        unique[key] = value
    groups = {}
    for (signature, _), value in unique.items():
        groups.setdefault(signature, []).append(value)
    # Even a stage without metering can have an outcome record; unknown is not zero usage.
    if not groups:
        groups[('unknown', 'unknown', 'text', 'token')] = []
    for signature, calls in groups.items():
        provider, model, modality, unit = signature
        row = {'id': str(uuid5(NAMESPACE_URL, json.dumps(['stage-usage-v1', stage, identity, *signature]))),
               'job_id': context.get('job_id'), 'job_type': stage,
               'provider': provider, 'model': model, 'modality': modality, 'billing_unit': unit,
               'call_status': 'completed' if state == 'completed' and
                   (not calls or any(call.get('call_status') in SUCCESS for call in calls)) else 'failed',
               'created_at': datetime.now(timezone.utc).isoformat()}
        partial = []
        for field in METRICS:
            values = [call.get(field) for call in calls]
            known = [value for value in values if type(value) in (int, float) and
                     math.isfinite(value) and value >= 0]
            row[field] = (sum(known) if all(type(value) is int for value in known) else math.fsum(known)) if known else None
            if known and len(known) != len(values):
                partial.append(field)
        row['raw_usage'] = {'aggregation': 'stage_provider_model_unit', 'stage_status': state,
                            'call_count': len(calls), 'partial_fields': partial, 'calls': calls}
        yield row


def submit_stage_usage(database, rows, context, *, state):
    """Bookkeeping must never prevent scenario, previs or video completion."""
    if context.get('usageLogEnabled') is not True or context.get('db_name') not in {'dev_user', 'aniestkh'}:
        return
    try:
        for row in aggregate_stage_usage(rows, context, state=state):
            database.submit(row, context)
    except Exception as error:
        logging.getLogger('uvicorn.error').warning(
            '[사용량 DB] 단계별 집계 기록 실패 · job=%s · %s', context.get('job_id'), type(error).__name__)
