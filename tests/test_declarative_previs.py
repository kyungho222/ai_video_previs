import asyncio
import ast
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ai_video.video_higgsfield_jutsu.python_backend.scene_contract import validate_scene, legacy_plan
from ai_video.video_higgsfield_jutsu.python_backend.scene_compiler import compile_scene
from ai_video.video_higgsfield_jutsu.python_backend.resources.scene_motion import sample_track
from ai_video.video_higgsfield_jutsu.python_backend.scene_pipeline import run_scene, plan_scene
from ai_video.model_gateway import routed_post


def scene():
    return {
        'description':'두 사람이 인사한다',
        'subjects':[
            {'id':'person_1','label':'첫 사람','kind':'human','shape':'box','color':[.2,.4,.8], 'size':[1,1,1], 'position':[-1,0,0], 'yaw':0},
            {'id':'person_2','label':'두번째 사람','kind':'human','shape':'box','color':[.8,.3,.2], 'size':[1,1,1], 'position':[1,0,0], 'yaw':0},
        ],
        'tracks':[
            {'subject':'person_1','action':'wave','start':0,'end':5,'position':[-1,0,0],'yaw':0,'target':None},
            {'subject':'person_2','action':'idle','start':0,'end':5,'position':[1,0,0],'yaw':0,'target':None},
        ],
        'scenes':[{'title':'인사','description':'한 사람만 손을 흔든다','start':0,'end':5,
                   'camera':{'focus':['person_1','person_2'],'framing':'full','movement':'locked','azimuth':0,'end_azimuth':0,'elevation':10}}],
        'background':[.12,.14,.18],
    }


def test_contract_and_downstream_timeline():
    p=validate_scene(scene(),5,1)
    from ai_video.video_higgsfield_jutsu.python_backend.previs_workers import validate_plan
    validate_plan(legacy_plan(p),5,1)


@pytest.mark.parametrize('elevation,accepted', [(-10,True),(70,True),(-10.01,False),(70.01,False)])
def test_camera_elevation_schema_matches_validation(elevation, accepted):
    from ai_video.video_higgsfield_jutsu.python_backend.scene_contract import CAMERA
    rule = CAMERA['properties']['elevation']
    assert rule['minimum'] == -10
    assert rule['maximum'] == 70
    p = scene()
    p['scenes'][0]['camera']['elevation'] = elevation
    if accepted:
        validate_scene(p,5,1)
    else:
        with pytest.raises(ValueError, match='elevation'):
            validate_scene(p,5,1)


@pytest.mark.parametrize('mutate',[
    lambda p:p['subjects'][0].update(id='bad id'),
    lambda p:p['subjects'][0]['position'].__setitem__(0,float('nan')),
    lambda p:p['subjects'][1].update(id='person_1'),
    lambda p:p['tracks'][0].update(subject='unknown'),
    lambda p:p['tracks'][0].update(action='exec'),
    lambda p:p['tracks'][0].update(end=6),
    lambda p:p['tracks'].append(copy.deepcopy(p['tracks'][0])),
    lambda p:p['scenes'][0]['camera'].update(focus=['unknown']),
    lambda p:p['scenes'][0].update(start=1),
    lambda p:p.update(code='import os'),
])
def test_invalid_scene_rejected(mutate):
    p=scene();mutate(p)
    with pytest.raises(ValueError):validate_scene(p,5,1)


def test_compiler_never_executes_model_strings():
    p=scene();p['subjects'][0]['label']='\\\"; __import__("os").system("bad") #'
    code=compile_scene(validate_scene(p,5,1),5,'16:9')
    tree=ast.parse(code)
    assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ('eval','exec') for n in ast.walk(tree))
    assert 'build_declarative_scene' in code


def test_tracks_are_independent_and_hold_last_pose():
    p=validate_scene(scene(),5,1)
    a=sample_track(p,'person_1',1)
    b=sample_track(p,'person_2',1)
    assert a['action']=='wave' and b['action']=='idle'
    assert a['position']==[-1,0,0] and b['position']==[1,0,0]
    p['tracks'][0].update(action='walk',end=2,position=[2,0,0])
    assert sample_track(p,'person_1',4)['position']==[2,0,0]
    assert sample_track(p,'person_1',4)['action']=='idle'


def test_selected_model_does_not_fallback():
    async def check():
        calls=[]
        def respond(req):
            calls.append(req)
            return httpx.Response(503,json={'error':{'code':'unavailable'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            result=await routed_post(http,'gpt-key','responses',{'model':'kimi','input':[]},
                                     config={'MOONSHOT_API_KEY':'test'},allow_fallback=False)
        assert result.status_code==503 and len(calls)==1
        assert calls[0].url.host=='api.moonshot.ai'
    asyncio.run(check())


def test_invalid_plan_repaired_by_same_model_without_tools():
    async def check():
        calls=[]
        async def request(path,body,**kwargs):
            calls.append(body)
            p=scene()
            if len(calls)==1:p['tracks'][0]['subject']='missing'
            return {'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(p)}]}]}
        job={'duration':5,'aspect':'16:9','request':'두 사람이 인사한다','scenarioTitles':['인사'],'chatModel':'gemma4-26b-a4b-it'}
        result=await plan_scene(request,job,[],lambda *a:None)
        assert result['subjects'][0]['id']=='person_1'
        assert len(calls)==2 and all(b['model']=='gemma4-26b-a4b-it' and 'tools' not in b for b in calls)
        assert 'missing' in json.dumps(calls[1],ensure_ascii=False)
    asyncio.run(check())


class Remote:
    status={'endpoint':'test','state':'connected'}
    definitions=[{'name':'scene_builder_3d_'+k} for k in ('create_project','list_projects','get_project','run_python','get_artifact')]
    def __init__(self,uncertain=False):self.calls=[];self.uncertain=uncertain
    async def call(self,name,args):
        kind=name.removeprefix('scene_builder_3d_');self.calls.append((kind,args))
        if kind=='create_project':
            self.name=args['name']
            if self.uncertain:return {'structuredContent':{'ok':False,'error':{'code':'PROJECT_CREATION_UNCONFIRMED','status':429}},'isError':True}
            data={'ok':True,'projectId':'project-one'}
        elif kind=='list_projects':data={'ok':True,'projects':[{'id':'project-one','name':self.name}]}
        elif kind=='get_project':data={'ok':True,'exists':False,'project':{'projectId':'project-one','revision':0,'sceneSequence':0}}
        elif kind=='run_python':data={'ok':True,'operation':{'id':args['operationId'],'projectId':'project-one','status':'succeeded','revisionAfter':1,'sceneSequenceAfter':0,'result':{'declarativeValidation':{'passed':True}}}}
        return {'structuredContent':data}


@pytest.mark.parametrize('uncertain',[False,True])
def test_server_owns_one_project_and_render(uncertain):
    async def check():
        client=Remote(uncertain)
        job={'id':'test-job','duration':5,'aspect':'16:9','media':[]}
        with patch('ai_video.video_higgsfield_jutsu.python_backend.scene_pipeline.finalize_current_previs',new=AsyncMock(return_value={'message':'done','media':[]})) as finalize:
            await run_scene(client,job,scene(),[],lambda *a:None)
        assert sum(k=='create_project' for k,a in client.calls)==1
        assert all(a.get('projectId','project-one')=='project-one' for k,a in client.calls)
        assert sum(k=='run_python' for k,a in client.calls)==1
        assert finalize.await_count==1
    asyncio.run(check())


def test_uncertain_creation_without_match_never_recreates():
    from ai_video.video_higgsfield_jutsu.python_backend.scene_pipeline import ensure_project
    async def check():
        client=Remote(True)
        original=client.call
        async def call(name,args):
            if name.endswith('list_projects'):
                client.calls.append(('list_projects',args))
                return {'structuredContent':{'ok':True,'projects':[]}}
            return await original(name,args)
        client.call=call
        job={'id':'uncertain-job'}
        for _ in range(2):
            with pytest.raises(RuntimeError,match='중복'):
                await ensure_project(client,job,lambda *a:None)
        assert sum(k=='create_project' for k,a in client.calls)==1
    asyncio.run(check())


def test_plan_failure_stops_before_remote_work():
    async def check():
        calls=[]
        async def request(path,body,**kwargs):
            calls.append(body['model'])
            return {'output':[]}
        job={'id':'bad','duration':5,'aspect':'16:9','request':'test','chatModel':'gemma4-26b-a4b-it'}
        with pytest.raises(RuntimeError,match='3회'):
            await plan_scene(request,job,[],lambda *a:None)
        assert calls==['gemma4-26b-a4b-it']*3
        assert 'sceneProjectId' not in job
    asyncio.run(check())


def test_repair_reuses_project_and_nonconfirmed_operation_is_not_replayed():
    async def check():
        client=Remote()
        job={'id':'repair-job','duration':5,'aspect':'16:9','media':[]}
        with patch('ai_video.video_higgsfield_jutsu.python_backend.scene_pipeline.finalize_current_previs',new=AsyncMock(return_value={'media':[]})):
            await run_scene(client,job,scene(),[],lambda *a:None)
            await run_scene(client,job,scene(),[],lambda *a:None)
        assert sum(k=='create_project' for k,a in client.calls)==1
        executions=[a for k,a in client.calls if k=='run_python']
        assert executions[0]['operationId']!=executions[1]['operationId']
    asyncio.run(check())


def test_prompt_vectors_are_fixed_size_for_constrained_decoders():
    from ai_video.video_higgsfield_jutsu.python_backend.scene_contract import SCENE_SCHEMA
    subject=SCENE_SCHEMA['properties']['subjects']['items']['properties']
    for key in ('color','size','position'):
        assert subject[key]['minItems']==subject[key]['maxItems']==3


def test_followup_keeps_scene_design_in_baseline():
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from ai_video.video_higgsfield_jutsu.python_backend.repositories import local_repositories
    with TemporaryDirectory() as folder:
        store=local_repositories(Path(folder)).previs_jobs
        # Repository-backed server store uses its existing version/snapshot contract.
        job,sid,_=store.create('한 사람이 손을 흔든다',duration=5)
        job['sceneDesign']=scene()
        job['codeOperations']=[{'operationId':'built','status':'succeeded'}]
        job['media']=[{'name':'previs.mp4','mediaType':'video/mp4'}]
        store.finish(job,{'message':'done'})
        follow,_,_=store.create('구도를 가깝게',sid,duration=5)
        assert follow['baseSnapshot']['sceneDesign']==scene()
