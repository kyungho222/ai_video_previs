"""Small structured asset logs without image data, URLs or request credentials."""
from collections import Counter
from copy import deepcopy
import json
import logging
import re
from pathlib import Path


def asset_rows(assets):
    return [{'index':i, 'assetId':a.get('id') or a.get('assetId'), 'role':a.get('role'),
             'name':a.get('name') or Path(str(a.get('path',''))).name,
             'label':a.get('label',''), 'sourceIndex':a.get('sourceIndex',i),
             **({'characterLayout':a.get('characterLayout','single')} if a.get('role')=='character' else {}),
             **({'analysisStatus':a['analysisStatus'],'analysisReason':a.get('analysisReason')} if 'analysisStatus' in a else {}),
             **({'sourceCrop':deepcopy(a['bbox'])} if 'bbox' in a else {})}
            for i,a in enumerate(assets,1)]


def log_asset_event(stage, *, job_id=None, **details):
    event={'stage':stage,'jobId':job_id,**details}
    logging.getLogger('uvicorn.error').info('[asset-diagnostics] %s',json.dumps(event,ensure_ascii=False))
    return event


def log_mapping_attempt(bundle, record, assets):
    candidate=record.get('candidate')
    entities=candidate.get('entities',[]) if isinstance(candidate,dict) else []
    entities=entities if isinstance(entities,list) else []
    rows=[{k:deepcopy(e.get(k)) for k in ('id','label','assetIndices','sourceObjectIds','confidence')}
          for e in entities if isinstance(e,dict)]
    received=[i for e in rows if isinstance(e.get('assetIndices'),list)
              for i in e['assetIndices'] if type(i) is int]
    refs=candidate.get('environmentReferences',[]) if isinstance(candidate,dict) else []
    refs=refs if isinstance(refs,list) else []
    received.extend(r['assetIndex'] for r in refs if isinstance(r,dict) and type(r.get('assetIndex')) is int)
    counts=Counter(received)
    raw=record.get('rawCandidate')
    raw_entities=raw.get('entities',[]) if isinstance(raw,dict) else []
    raw_entities=raw_entities if isinstance(raw_entities,list) else []
    returned=[i for e in raw_entities if isinstance(e,dict) and isinstance(e.get('assetIndices'),list)
              for i in e['assetIndices'] if type(i) is int]
    slots=raw.get('assetMappings',{}) if isinstance(raw,dict) else {}
    slot_keys=list(slots) if isinstance(slots,dict) else []
    if isinstance(slots,dict):
        returned.extend(int(key.split('_')[1]) for key in slots
                        if isinstance(key,str) and re.fullmatch(r'asset_[1-9][0-9]*',key))
    return log_asset_event('mapping_attempt',job_id=bundle.get('jobId'),attempt=record.get('attempt'),
        mode=record.get('mode'),targetAssetIndices=record.get('targetAssetIndices',[]),
        requiredAssetKeys=record.get('requiredAssetKeys',[]),returnedAssetKeys=slot_keys,
        expectedAssetIndices=list(range(1,len(assets)+1)),receivedAssetIndices=received,
        missingAssetIndices=sorted(set(range(1,len(assets)+1))-set(received)),
        duplicateAssetIndices=sorted(i for i,n in counts.items() if n>1),
        unexpectedAssetIndices=sorted(i for i in counts if not 1<=i<=len(assets)),
        rawReturnedAssetIndices=returned,entities=rows,error=record.get('issue'),fieldIssues=record.get('fieldIssues',[]))
