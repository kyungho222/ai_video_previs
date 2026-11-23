"""Compile validated scene data with packaged, reviewed Blender code only."""
import json
from pathlib import Path
from .scene_contract import validate_scene


def compile_scene(plan, duration, aspect):
    plan = validate_scene(plan, duration)
    resources = Path(__file__).with_name('resources')
    source = '\n'.join((resources / name).read_text('utf-8')
                       for name in ('scene_motion.py', 'scene_runtime.py'))
    # repr(JSON) is a Python literal: labels and descriptions can never become source.
    return ('import json\n' + source + '\n_scene_plan=json.loads('
            + repr(json.dumps(plan, ensure_ascii=False, allow_nan=False)) + ')\n'
            + 'result=build_declarative_scene(_scene_plan,' + repr(duration) + ',' + repr(aspect) + ')\n')
