"""Real Blender integration for Python-owned framing and independent motion."""
import copy
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from test_declarative_previs import scene
from ai_video.video_higgsfield_jutsu.python_backend.scene_compiler import compile_scene
from ai_video.video_higgsfield_jutsu.python_backend.cloud_python import prepare_cloud_python

BLENDER=shutil.which('blender') or os.getenv('BLENDER_BIN')
if not BLENDER:
    default=Path('C:/Program Files/Blender Foundation/Blender 5.2/blender.exe')
    if default.is_file():BLENDER=str(default)


@pytest.mark.skipif(not BLENDER,reason='Blender CLI not installed')
def test_real_blender_camera_and_occlusion(tmp_path):
    p=scene()
    p['tracks'][0].update(action='walk',position=[0,1,0])
    second=copy.deepcopy(p['scenes'][0]);p['scenes'][0]['end']=2.5;second['start']=2.5;p['scenes'].append(second)
    code=prepare_cloud_python(compile_scene(p,5,'9:16'))
    code+='''
import bpy
assert result['declarativeValidation']['passed']
_camera=bpy.context.scene.camera
_positions=[]
for _f in (1,60,61,120):
    bpy.context.scene.frame_set(_f)
    _positions.append(tuple(_camera.location))
assert all(sum((a-b)**2 for a,b in zip(_positions[0],p))<1e-6 for p in _positions)
assert bpy.context.scene.render.resolution_y>bpy.context.scene.render.resolution_x
'''
    q=scene();q['tracks'][0].update(action='walk',position=[0,1,0]);q['scenes'][0]['camera']['movement']='pan'
    code+='\n'+prepare_cloud_python(compile_scene(q,5,'16:9'))
    code+='''
_camera=bpy.context.scene.camera
bpy.context.scene.frame_set(1)
_pos=tuple(_camera.location);_rot=tuple(_camera.rotation_euler)
bpy.context.scene.frame_set(120)
assert sum((a-b)**2 for a,b in zip(_pos,_camera.location))<1e-6
assert sum((a-b)**2 for a,b in zip(_rot,_camera.rotation_euler))>1e-6
'''
    wall=scene();wall['subjects'].append({'id':'wall','label':'wall','kind':'prop','shape':'box',
        'color':[.3,.3,.3],'size':[8,.4,5],'position':[0,2,0],'yaw':0})
    code+='\n'+prepare_cloud_python(compile_scene(wall,5,'16:9'))
    code+='''
assert not result['declarativeValidation']['passed'],result
assert any('wall' in row['occluders'] or 'wall_mesh' in row['occluders'] for row in result['declarativeValidation']['visibility']['subjects']),result
print('DECLARATIVE_INTEGRATION_OK')
'''
    script=tmp_path/'scene.py';script.write_text(code,'utf-8')
    completed=subprocess.run([BLENDER,'--factory-startup','-b','--threads','2','--python-exit-code','1','--python',str(script)],
                             capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
    assert completed.returncode==0,completed.stdout[-4000:]+completed.stderr[-4000:]
    assert 'DECLARATIVE_INTEGRATION_OK' in completed.stdout
