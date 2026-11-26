"""Endpoint-facing usage registration, independent of scene/video generation.

Routes call request preparation and terminal recording hooks only. Member lookup,
ownership snapshots, stage naming, aggregation and delivery belong here or in
the private metering/storage modules used below.
"""
import asyncio
from dataclasses import dataclass, field
import logging

from .stage_usage import submit_stage_usage
from .usage_database import UsageDatabase, resolve_member_id
from .wan_pricing import price_wan_usage
from .work_settings import request_work_metadata

logger = logging.getLogger('uvicorn.error')
SCENARIO = 'shortform_scenario'
PREVIS = 'shortform_previs'
VIDEO = 'shortform_video'
TERMINAL = {'completed', 'failed'}


def registration_options(body):
    enabled = body.get('usageLogEnabled') is True
    return {'usageLogEnabled': enabled, 'usageLogOverride': enabled and body.get('usageLogOverride', True) is True}


def create_usage_database(config, runtime_root):
    return UsageDatabase(config, runtime_root / 'runtime/data/usage-db-outbox.sqlite3')


@dataclass
class ScenarioUsage:
    database: object
    context: dict
    rows: list = field(default_factory=list)

    def capture(self, row):
        if isinstance(row, dict):
            self.rows.append(row)

    @property
    def snapshot_fields(self):
        if not self.context['usageLogEnabled'] and not self.context.get('mb_id'):
            return {}
        return {'usageMemberId': self.context['usageMemberId'], 'usageJobId': self.context['job_id']}

    def annotate_result(self, result):
        # Some providers only expose metering in the returned response.
        for row in result.get('ai_usage', []):
            if isinstance(row, dict) and row not in self.rows:
                self.rows.append(row)
        result['ai_usage'] = [{**row, **{key: self.context[key] for key in
            ('job_id', 'session_id', 'request_id', 'job_type')}} for row in self.rows]

    def finish(self, state):
        submit_stage_usage(self.database, self.rows, self.context, state=state)


async def begin_scenario_usage(service, body, *, job_id, session_id, request_id):
    context = {**request_work_metadata(body), **registration_options(body),
               'job_id': job_id, 'session_id': session_id, 'request_id': request_id, 'job_type': SCENARIO}
    context['usageMemberId'] = (await asyncio.to_thread(resolve_member_id, context, service.config)
                                if context['usageLogEnabled'] else context.get('mb_id'))
    return ScenarioUsage(service.usage_database, context)


async def prepare_previs_usage(service, job, body, *, scenario, metadata, request_id):
    job.update(**registration_options(body),
               usageDbName=scenario['settings'].get('db_name') if scenario is not None else body.get('db_name'),
               requestId=request_id)
    if scenario is not None:
        job['usageJobId'] = scenario.get('usageJobId') or scenario.get('job_id')
    job['usageMemberId'] = (scenario['usageMemberId'] if scenario is not None and 'usageMemberId' in scenario
                            else await asyncio.to_thread(resolve_member_id, {**metadata, **registration_options(body)}, service.config)
                            if body.get('usageLogEnabled') is True else metadata.get('mb_id'))


def record_previs_usage(service, job_id):
    try:
        snapshot = service.store.get(job_id)
        if not snapshot or snapshot['state'] not in TERMINAL:
            return
        job = snapshot['data']
        settings = job.get('workSettings') or {}
        context = {**settings, 'db_name': job.get('usageDbName') or settings.get('db_name'),
                   'usageLogEnabled': job.get('usageLogEnabled') is True,
                   'usageLogOverride': job.get('usageLogOverride') is True,
                   'job_id': job.get('usageJobId') or job_id, 'execution_job_id': job_id,
                   'session_id': job.get('sessionId'),
                   'request_id': job.get('requestId'), 'job_type': PREVIS}
        if 'usageMemberId' in job:
            context['usageMemberId'] = job['usageMemberId']
        rows = [event['value'] for event in snapshot['events'] if event['type'] == 'ai_usage']
        submit_stage_usage(service.usage_database, rows, context, state=snapshot['state'])
    except Exception as error:
        logger.warning('[사용량 DB] 프리비즈 집계 지연 · job=%s · %s', job_id, type(error).__name__)


async def prepare_video_usage(service, options, body, *, source, previous_job, settings, metadata, request_id):
    options['usageJobId'] = ((previous_job or {}).get('options', {}).get('usageJobId')
                             or source.get('usageJobId') or source.get('jobId') or source.get('id'))
    options.update(**registration_options(body),
                   usageDbName=settings.get('db_name') if settings is not None else body.get('db_name'),
                   usageRequestId=previous_job['options'].get('usageRequestId') if previous_job else request_id,
                   usageSessionId=source.get('sessionId') or (previous_job or {}).get('options', {}).get('usageSessionId'))
    if previous_job and 'usageMemberId' in previous_job['options']:
        options['usageMemberId'] = previous_job['options']['usageMemberId']
    elif 'usageMemberId' in source:
        options['usageMemberId'] = source['usageMemberId']
    elif previous_job is None and body.get('usageLogEnabled') is True:
        options['usageMemberId'] = await asyncio.to_thread(
            resolve_member_id, {**(settings or metadata), **registration_options(body)}, service.config)


def record_video_usage(manager, job_id):
    try:
        current = manager.store.get(job_id)
        if not current or current['state'] not in TERMINAL:
            return
        options = current['options']
        if options.get('usageLogEnabled') is not True:
            return
        source_id = current.get('source', {}).get('jobId')
        source = manager.service.usage_history.get(source_id) if source_id else None
        if not source:
            from .camera_reference import source_scene_reference
            source = source_scene_reference(manager.service, current.get('source', {})) or {}
        settings = options.get('workSettings') or source.get('workSettings') or {}
        context = {**settings, 'db_name': options.get('usageDbName') or settings.get('db_name'),
                   'usageLogEnabled': True, 'usageLogOverride': options.get('usageLogOverride') is True,
                   'job_id': options.get('usageJobId') or source.get('usageJobId') or source_id or job_id,
                   'execution_job_id': job_id, 'source_job_id': source_id,
                   'session_id': options.get('usageSessionId') or source.get('sessionId'), 'request_id': options.get('usageRequestId'), 'job_type': VIDEO}
        if 'usageMemberId' in options:
            context['usageMemberId'] = options['usageMemberId']
        elif 'usageMemberId' in source:
            context['usageMemberId'] = source['usageMemberId']
        rows = [price_wan_usage(row, resolution=options.get('resolution'), duration=options.get('duration'))
                for row in current['metadata'].get('ai_usage', [])]
        submit_stage_usage(manager.service.usage_database, rows, context,
                           state=current['state'])
    except Exception as error:
        logger.warning('[사용량 DB] 영상 집계 지연 · job=%s · %s', job_id, type(error).__name__)
