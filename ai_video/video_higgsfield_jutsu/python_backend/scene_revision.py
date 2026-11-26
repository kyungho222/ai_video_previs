"""Explicit revision provenance and structural changes; no semantic success claims."""
from uuid import UUID


def resolve_revision_baseline(service, source_job_id, session_id=None):
    try:
        source_job_id=str(UUID(source_job_id))
    except (ValueError,TypeError,AttributeError):
        raise ValueError('수정할 sourceJobId 형식이 올바르지 않습니다.') from None
    row=service.store.get(source_job_id)
    if row is not None:
        if row['state']!='completed':
            raise ValueError('완료된 프리비즈만 수정할 수 있습니다.')
        data=row['data']; version=data.get('version'); saved_session=row.get('session') or data.get('sessionId')
    else:
        data=service.archive.scene_reference(source_job_id)
        version=data.get('sceneVersion'); saved_session=data.get('sessionId')
    if not data.get('sceneDesign'):
        raise ValueError('수정 기준 프리비즈의 장면 정보가 없거나 만료됐습니다. 프리비즈를 다시 선택하세요.')
    if session_id and saved_session and session_id!=saved_session:
        raise ValueError('수정 대상 프리비즈와 sessionId가 일치하지 않습니다.')
    return {**{key:data.get(key) for key in ('scenePrompt','sceneDesign','sceneDirection','sceneState','scenarioTitles',
             'remoteProjects','duration','aspect','plan','workSettings')},
            'id':source_job_id,'sceneId':data.get('sceneId') or source_job_id,
            'version':version,'sessionId':saved_session}


def revision_diagnostics(baseline, execution, request):
    before=baseline.get('sceneDesign') or {}
    a={s['id']:s for s in before.get('subjects',[])}
    b={s['id']:s for s in execution.get('subjects',[])}
    changed=sorted(key for key in a.keys() & b.keys() if a[key]!=b[key])
    removed=sorted(a.keys()-b.keys()); added=sorted(b.keys()-a.keys())
    actions=before.get('tracks')!=execution.get('tracks')
    cameras=before.get('scenes')!=execution.get('scenes')
    return {'sourceJobId':baseline.get('id'),'baseVersion':baseline.get('version'),
            'request':request,'changedObjectIds':changed,'removedObjectIds':removed,'addedObjectIds':added,
            'actionsChanged':actions,'shotsChanged':cameras,
            'status':'structural_changes_detected' if changed or removed or added or actions or cameras else 'no_structural_change',
            'semanticStatus':'not_verified'}
