import ast
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_video.video_higgsfield_jutsu.python_backend.cloud_python import RESOURCES

RESOURCE = RESOURCES / 'stage_motion.py'

class Vector:
    def __init__(self, xyz): self.xyz = tuple(xyz)
    def __add__(self, other): return Vector(a+b for a,b in zip(self.xyz, other.xyz))
    def __sub__(self, other): return Vector(a-b for a,b in zip(self.xyz, other.xyz))
    def __mul__(self, value): return Vector(a*value for a in self.xyz)
    def __truediv__(self, value): return Vector(a/value for a in self.xyz)
    @property
    def length(self): return math.sqrt(sum(a*a for a in self.xyz))
    def copy(self): return Vector(self.xyz)
    def __getitem__(self, key): return self.xyz[key]

class Matrix:
    def __init__(self, xyz): self.translation = Vector(xyz)
    def __matmul__(self, other): return self.translation + other

class Object(dict):
    def __init__(self, name, xyz=(0,0,0), parent=None, kind='MESH'):
        super().__init__();self.name=name;self.parent=parent;self.type=kind
        self.matrix_world=Matrix(xyz);self.hide_render=False;self.original=self
        self.bound_box=[(-.1,-.1,-.1),(.1,.1,.1)]
    def __eq__(self, other): return self is other
    def __hash__(self): return id(self)
    def evaluated_get(self, dg): return self

class Objects(list):
    def get(self, name): return next((o for o in self if o.name==name),None)

class VisibilityTests(unittest.TestCase):
    def run_check(self, blocked=True, hidden=False, block_frames=None, points=None):
        scope={};exec(compile(RESOURCE.read_text(encoding='utf-8'),'camera_visibility','exec'),scope)
        root=Object('Dancer',kind='EMPTY');root['previs_motion_version']='registered'
        head=Object('Dancer_Head',(10,0,1.8),root)
        torso=Object('Dancer_Torso',(10,0,1),root)
        hand=Object('Dancer_L_Hand',(10,0,1.2),root)
        root['previs_motion_markers']=json.dumps({'L_Hand':hand.name})
        camera=Object('Camera',(0,0,1.5),kind='CAMERA')
        camera['previs_framing_subjects']=json.dumps([root.name])
        wall=Object('Wall',(5,0,0));wall.hide_render=hidden
        scene=SimpleNamespace(camera=camera, objects=Objects([root,head,torso,hand,wall]),
                              frame_start=1,frame_end=24,frame_current=7,
                              render=SimpleNamespace(fps=24,fps_base=1))
        def frame_set(frame):scene.frame_current=frame
        scene.frame_set=frame_set
        def ray_cast(dg, origin, direction, distance):
            active=blocked and (block_frames is None or scene.frame_current in block_frames)
            if active and origin[0]<5 and (points is None or points(direction)):
                scale=(5-origin[0])/direction[0]
                return True,origin+direction*scale,None,0,wall,None
            return False,None,None,None,None,None
        scene.ray_cast=ray_cast
        issues={}
        def quality(condition,message,check='camera',key=None):
            if condition:issues.pop(key,None)
            else:issues[key]=message
        scope['previs_quality_check']=quality
        with patch.dict(sys.modules,{'bpy':SimpleNamespace(context=SimpleNamespace(scene=scene,evaluated_depsgraph_get=lambda:object())),
                                     'mathutils':SimpleNamespace(Vector=Vector)}):
            report=scope['previs_validate_visibility']({})
        self.assertEqual(scene.frame_current,7)
        return report,issues

    def test_stationary_camera_behind_wall_is_detected(self):
        report,issues=self.run_check()
        self.assertFalse(report['passed']);self.assertIn('camera-line-of-sight',issues)
        self.assertEqual(report['subjects'][0]['blockedBodyFrames'],24)
        self.assertIn('Wall',report['subjects'][0]['occluders'])

    def test_clear_view_passes(self):
        report,issues=self.run_check(blocked=False)
        self.assertTrue(report['passed']);self.assertFalse(issues)

    def test_hidden_wall_is_ignored(self):
        report,_=self.run_check(hidden=True)
        self.assertTrue(report['passed'])

    def test_brief_crossing_is_reported_without_stopping(self):
        report,issues=self.run_check(block_frames={2,3})
        self.assertTrue(report['passed']);self.assertFalse(issues)
        self.assertEqual(report['subjects'][0]['blockedBodyFrames'],2)

    def test_separated_brief_occlusions_do_not_become_sustained(self):
        report,issues=self.run_check(block_frames=set(range(1,25,2)))
        self.assertTrue(report['passed']);self.assertFalse(issues)
        self.assertEqual(report['subjects'][0]['blockedBodyFrames'],12)
        self.assertEqual(report['subjects'][0]['longestBlockedFrames'],1)

    def test_half_second_of_sustained_occlusion_is_flagged(self):
        report,issues=self.run_check(block_frames=set(range(1,13)))
        self.assertFalse(report['passed']);self.assertTrue(issues)

    def test_hand_occlusion_is_recorded(self):
        report,_=self.run_check()
        self.assertGreater(report['subjects'][0]['handBlockedFrames']['L_Hand'],0)

    def test_legacy_resource_list_supplies_visibility_definition(self):
        resources = RESOURCE.parent
        legacy_names = ('cloud_prelude.py','motion_library.py','object_motion.py',
                        'camera_reference.py','camera_orientation.py','stage_motion.py',
                        'camera_scale.py','vehicle_motion.py','dog_mirror.py')
        code = ''.join((resources/name).read_text(encoding='utf-8') for name in legacy_names)
        tree = ast.parse(code)
        definitions={n.name for n in tree.body if isinstance(n,ast.FunctionDef)}
        self.assertIn('previs_validate_visibility',definitions)
        compile(code,'<legacy-cloud-injection>','exec')

    def test_final_render_and_ai_review_use_visibility_check(self):
        base=RESOURCE.parent.parent
        tree=ast.parse((base/'previs_finalization.py').read_text(encoding='utf-8'))
        assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CAMERA_QUALITY_REPORT_CODE' for t in n.targets))
        self.assertIn('previs_validate_visibility',ast.literal_eval(assignment.value))
        prelude=(base/'resources/cloud_prelude.py').read_text(encoding='utf-8')
        self.assertIn('"visibilityValidation": visibility_report',prelude)

if __name__ == '__main__':unittest.main()
