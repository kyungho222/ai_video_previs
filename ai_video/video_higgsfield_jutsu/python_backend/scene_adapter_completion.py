"""Bounded, evidence-backed partial completion; never requests a full scene."""
import json
import re
import httpx
from copy import deepcopy
from .contract_validation import contract_issues
from .scene_response_adapter import extract_scene_object

MAX_FIELDS, MAX_INPUT_CHARS, MAX_OUTPUT_TOKENS = 6, 6000, 1200


async def request_missing_fields(request, job, items, audit):
    if not items or job.get('sceneAdapterCompletion',{}).get('used',0):
        return {}
    if any(not item.get('context',{}).get('source','').strip() for item in items):
        job['sceneAdapterCompletion']={'used':0,'status':'skipped_missing_source'}
        return {}
    if len(items)>MAX_FIELDS:
        job['sceneAdapterCompletion']={'used':0,'status':'skipped_field_limit'}
        return {}
    fields={}; payload=[]
    for i,item in enumerate(items):
        nullable=deepcopy(item['schema'])
        types=nullable.get('type')
        nullable['type']=list(dict.fromkeys((types if isinstance(types,list) else [types])+['null']))
        if 'enum' in nullable: nullable['enum'].append(None)
        fields[f'field_{i}']={'type':'object','properties':{'confirmed':{'type':'boolean'},'value':nullable,
            'evidence':{'type':'string','maxLength':240}},'required':['confirmed','value','evidence'],'additionalProperties':False}
        payload.append({'field':f'field_{i}','path':item['path'],'context':item['context']})
    text=json.dumps(payload,ensure_ascii=False)
    output_schema={'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}
    input_chars=len(text)+len(json.dumps(output_schema,ensure_ascii=False))+500
    if input_chars>MAX_INPUT_CHARS:
        job['sceneAdapterCompletion']={'used':0,'status':'skipped_input_limit'}
        return {}
    record={'used':1,'model':job['chatModel'],'status':'requested','paths':[i['path'] for i in items],
            'inputChars':input_chars,'maxOutputTokens':MAX_OUTPUT_TOKENS,'applied':[]}
    job['sceneAdapterCompletion']=record
    try:
        response=await request('responses',{'model':job['chatModel'],'max_output_tokens':MAX_OUTPUT_TOKENS,
            'instructions':'Complete ONLY the requested missing fields from supplied source evidence. '
            'Do not invent values or redesign the scene. If evidence is insufficient, confirmed=false and value=null. '
            'For confirmed values, evidence must be a verbatim excerpt of context.source.',
            'input':text,'text':{'format':{'type':'json_schema','name':'scene_adapter_completion','strict':True,
              'schema':output_schema}}},
            stage_label='장면 어댑터 필수값 보완')
        value=extract_scene_object(response)
        record['rawResponse']=deepcopy(value)
        if contract_issues(value,{'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}):
            record['status']='invalid_partial_response'
            return {}
        patches={}
        for i,item in enumerate(items):
            answer=value[f'field_{i}']; evidence=answer['evidence']
            numbers = answer['value'] if isinstance(answer['value'],list) else [answer['value']]
            numeric = [float(v) for v in numbers if type(v) in (int,float)]
            source_numbers = [float(v) for v in re.findall(r'-?[0-9]+(?:\.[0-9]+)?', evidence)]
            if numeric and any(v not in source_numbers for v in numeric):
                continue
            if (answer['confirmed'] is True and evidence.strip() and evidence in item['context'].get('source','')
                    and not contract_issues(answer['value'],item['schema'])):
                patches[item['path']]=deepcopy(answer['value'])
        record.update(status='completed' if patches else 'unconfirmed',applied=list(patches))
        return patches
    except (ValueError,TypeError,KeyError,AttributeError,RuntimeError,httpx.HTTPError) as error:
        record.update(status='failed',errorType=type(error).__name__)
        return {}


def missing_field_items(error, raw, source, schema):
    """Only structurally missing leaf fields qualify; never repair arbitrary paths."""
    items=[]
    for issue in getattr(error,'issues',[]):
        if issue.get('message')!='required field missing':
            continue
        path=issue.get('path','')
        tokens=re.findall(r'([A-Za-z_][A-Za-z_0-9]*)|\[([0-9]+)\]',path)
        node,spec=raw,schema
        try:
            for offset,(key,index) in enumerate(tokens):
                if key:
                    spec=spec['properties'][key]
                    if offset<len(tokens)-1: node=node[key]
                else:
                    spec=spec['items'];node=node[int(index)]
            # Missing whole objects/lists would effectively request a redesign.
            if spec.get('type')=='object' or (spec.get('type')=='array' and spec.get('items',{}).get('type')=='object'):
                continue
            label=node.get('label','') if isinstance(node,dict) else ''
            excerpt=str(source or '')[:1200]
            numeric = spec.get('type') in ('number','integer') or (spec.get('type')=='array' and spec.get('items',{}).get('type') in ('number','integer'))
            if numeric and not re.search(r'[0-9]',excerpt):
                continue
            items.append({'path':path,'schema':deepcopy(spec),'context':{'label':label,'source':excerpt}})
        except (KeyError,TypeError,IndexError):
            continue
    return items


def apply_field_patches(raw, patches):
    result=deepcopy(raw)
    for path,value in patches.items():
        tokens=re.findall(r'([A-Za-z_][A-Za-z_0-9]*)|\[([0-9]+)\]',path)
        node=result
        for key,index in tokens[:-1]: node=node[key] if key else node[int(index)]
        key,index=tokens[-1]
        if key: node[key]=deepcopy(value)
        else: node[int(index)]=deepcopy(value)
    return result


async def adapt_with_completion(request, response, job, *, source, duration, titles=()):
    from .common_scene_adapter import adapt_scene_response
    from .scene_direction import DIRECTION_SCHEMA
    try:
        return adapt_scene_response(response,request=source,duration=duration,titles=titles)
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        trace=getattr(error,'scene_adapter_trace',None)
        if not trace:
            raise
        raw=trace.get('normalized') or trace['raw']
        items=missing_field_items(error,raw,source,DIRECTION_SCHEMA)
        patches=await request_missing_fields(request,job,items,{})
        if not patches:
            raise
        patched=apply_field_patches(raw,patches)
        adapted=adapt_scene_response({'structuredContent':patched},request=source,duration=duration,titles=titles)
        adapted['raw']=trace['raw']
        adapted['changes']=[*trace['changes'],*adapted['changes'],
                            *[{'path':path,'after':value,'reason':'evidence_backed_model_supplement'}
                              for path,value in patches.items()]]
        return adapted
