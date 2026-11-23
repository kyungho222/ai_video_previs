"""Server-owned Blender renderer for the declarative scene contract.

This file is packaged into a cloud operation, not imported by the web process.
Characters are readable blocking proxies, not finished character assets.
"""


def _scene_primitive(name, shape, position, size, color, parent=None):
    import bpy
    if shape == 'sphere':
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=.5)
    elif shape == 'cylinder':
        bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=.5, depth=1)
    else:
        bpy.ops.mesh.primitive_cube_add(size=1)
    obj = bpy.context.object
    obj.name, obj.parent, obj.location, obj.scale = name, parent, position, size
    mat = bpy.data.materials.new(name + '_material')
    mat.diffuse_color = (*color, 1)
    obj.data.materials.append(mat)
    return obj


def _scene_animal(subject):
    import bpy
    root = bpy.data.objects.new(subject['id'], None)
    bpy.context.collection.objects.link(root)
    parts = {}
    bird = subject['kind'] == 'bird'
    def part(name, position, size):
        parts[name] = _scene_primitive(subject['id'] + '_' + name, 'sphere', position, size, subject['color'], root)
    part('body', (0, 0, .55), (.6, .9, .5))
    part('head', (0, .48, .85), (.38, .38, .38))
    part('nose', (0, .67, .83), (.18, .25, .15))
    part('tail', (0, -.54, .65), (.1, .4, .1))
    for side, sign in (('L', -1), ('R', 1)):
        part(side + '_ear', (sign * .14, .48, 1.06), (.1, .12, .22))
        for end, y in (('front', .28), ('back', -.28)):
            part(side + '_' + end, (sign * .23, y, .25), (.12, .16, .5))
        if bird:
            part(side + '_wing', (sign * .4, 0, .6), (.65, .35, .08))
    return {'root': root, 'parts': parts}


def _scene_apply_human(rig, sample, frame, target=None):
    from mathutils import Vector
    pose = human_pose(sample)
    root, parts = rig['root'], rig['parts']
    root.location = sample['position']
    root.location.z += pose['jump']
    root.rotation_euler = (pose['fall'] * _scene_math.pi / 2, 0, _scene_math.radians(sample['yaw']))
    # Rotate around the grounded pelvis while falling, rather than lifting the feet.
    root.location.z += .1 * pose['fall']
    _pv_key(root, frame)
    parts['Torso'].location.z = 1.25 - pose['crouch']
    parts['Pelvis'].location.z = .93 - pose['crouch']
    for name in ('Head', 'Nose', 'L_Eye', 'R_Eye', 'L_Pupil', 'R_Pupil'):
        base = 1.80 if 'Eye' in name or 'Pupil' in name else 1.76
        parts[name].location.z = base - pose['crouch']
        _pv_key(parts[name], frame)
    _pv_key(parts['Torso'], frame)
    _pv_key(parts['Pelvis'], frame)
    for side, sign in (('L', -1), ('R', 1)):
        j = pose['joints']
        hip, foot = Vector(j[side + '_Hip']), Vector(j[side + '_Foot'])
        shoulder, hand = Vector(j[side + '_Shoulder']), Vector(j[side + '_Hand'])
        if side == 'R' and target is not None and sample['action'] in ('reach', 'hold', 'point', 'slap'):
            # World target is converted into this actor's coordinates, including actor scale.
            local = root.matrix_world.inverted() @ Vector(target)
            blend = min(1, sample['phase'] * 3)
            hand = hand.lerp(local, blend)
        knee, foot = (Vector(p) for p in _pv_two_bone(hip, foot, .5, .5, (0, 1, 0)))
        elbow, hand = (Vector(p) for p in _pv_two_bone(shoulder, hand, .30, .28, (sign, .2, -.2)))
        for label, a, b, radius in (
            (side + '_UpperLeg', hip, knee, .072), (side + '_LowerLeg', knee, foot, .058),
            (side + '_UpperArm', shoulder, elbow, .055), (side + '_LowerArm', elbow, hand, .045),
        ):
            obj = parts[label]
            delta = b - a
            obj.location = (a + b) / 2
            obj.scale = (radius, radius, max(.001, delta.length / 2))
            obj.rotation_euler = delta.to_track_quat('Z', 'Y').to_euler()
            _pv_key(obj, frame)
        for label, point in (('Hip', hip), ('Knee', knee), ('Foot', foot),
                             ('Shoulder', shoulder), ('Elbow', elbow), ('Hand', hand)):
            if side + '_' + label in parts:
                parts[side + '_' + label].location = point
                _pv_key(parts[side + '_' + label], frame)


def _scene_bounds(roots, scene, framing='full'):
    import bpy
    from mathutils import Vector
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for root in roots:
        meshes = [o for o in [root, *root.children_recursive] if o.type == 'MESH' and not o.hide_render]
        current = [o.evaluated_get(depsgraph).matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
        if not current:
            raise ValueError('visible subject geometry is missing')
        lo = Vector(tuple(min(p[i] for p in current) for i in range(3)))
        hi = Vector(tuple(max(p[i] for p in current) for i in range(3)))
        # Close/medium target actual upper geometry, never the subject's ground-level origin.
        if framing in ('medium', 'close'):
            fraction = .55 if framing == 'medium' else .24
            lo.z = hi.z - (hi.z - lo.z) * fraction
            if framing == 'close':
                center_x = (lo.x + hi.x) / 2
                half = max(.12, (hi.z - lo.z) * .7)
                lo.x, hi.x = center_x - half, center_x + half
        points.extend(Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z))
    return points


def _scene_camera_pose(camera, scene, points, setup, phase):
    from mathutils import Vector
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    target = (low + high) / 2
    angle = setup['azimuth']
    if setup['movement'] == 'orbit':
        angle += (setup['end_azimuth'] - angle) * phase
    az, el = _scene_math.radians(angle), _scene_math.radians(setup['elevation'])
    # +Y is the subject's visible front. Zero azimuth looks from +Y toward the subject.
    outward = Vector((_scene_math.sin(az) * _scene_math.cos(el),
                      _scene_math.cos(az) * _scene_math.cos(el), _scene_math.sin(el)))
    rotation = (-outward).to_track_quat('-Z', 'Y')
    inverse = rotation.inverted()
    corners = camera.data.view_frame(scene=scene)
    tan_x = max(abs(p.x / p.z) for p in corners)
    tan_y = max(abs(p.y / p.z) for p in corners)
    margin = .70 if setup['framing'] == 'wide' else .86
    distance = .5
    for p in points:
        local = inverse @ (p - target)
        distance = max(distance, local.z + abs(local.x) / (tan_x * margin),
                       local.z + abs(local.y) / (tan_y * margin))
    if setup['movement'] == 'push':
        distance *= 1.3 - .3 * phase
    return target + outward * distance, target


def _scene_build_camera(scene, rigs, plan):
    import bpy
    from mathutils import Vector
    from bpy_extras.object_utils import world_to_camera_view
    camera = bpy.data.objects.new('CAM_main', bpy.data.cameras.new('CAM_main'))
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.data.lens = 50
    camera.data.clip_start, camera.data.clip_end = .01, 500
    setups = plan['scenes']
    previous_rotation = None
    previous_position = previous_target = None
    locked = {}
    camera_samples = []
    for frame in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(frame)
        time = (frame - 1) / 24
        index = next((i for i, s in enumerate(setups) if time < s['end']), len(setups) - 1)
        beat = setups[index]
        setup = beat['camera']
        roots = [rigs[key]['root'] for key in setup['focus']]
        points = _scene_bounds(roots, scene, setup['framing'])
        u = max(0, min(1, (time - beat['start']) / (beat['end'] - beat['start'])))
        if setup['movement'] in ('locked', 'pan'):
            lock_key = json.dumps(setup, sort_keys=True)
            if lock_key not in locked:
                # Fit the entire interval's moving envelope once; a fixed shot stays fixed.
                # Identical locked setups across scenario cards are one continuous shot.
                envelope = []
                intervals = [s for s in setups if s['camera'] == setup]
                frames = [f for s in intervals for f in range(
                    max(1, int(s['start'] * 24) + 1), min(scene.frame_end, int(s['end'] * 24)) + 1)]
                for f in frames:
                    scene.frame_set(f)
                    envelope.extend(_scene_bounds(roots, scene, setup['framing']))
                scene.frame_set(frame)
                locked[lock_key] = _scene_camera_pose(camera, scene, envelope, setup, 0)
            position, target = locked[lock_key]
            if setup['movement'] == 'pan':
                from mathutils import Vector
                # A pan rotates about a fixed camera origin to follow the focus geometry.
                low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
                high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
                target = (low + high) / 2
        else:
            position, target = _scene_camera_pose(camera, scene, points, setup, u)
        # No unrequested cuts at scenario boundaries. Blend pose then refit the current ROI.
        if index and time - beat['start'] < .3 and previous_position is not None:
            alpha = min(1, (time - beat['start'] + 1 / 24) / .3)
            position = previous_position.lerp(position, alpha)
            target = previous_target.lerp(target, alpha)
        rotation = (target - position).to_track_quat('-Z', 'Y')
        # Ensure all intended focus geometry is inside frame even through transitions.
        for _ in range(16):
            camera.location = position
            camera.rotation_euler = rotation.to_euler('XYZ', previous_rotation) if previous_rotation is not None else rotation.to_euler('XYZ')
            bpy.context.view_layer.update()
            views = [world_to_camera_view(scene, camera, point) for point in points]
            if all(p.z > 0 and .02 <= p.x <= .98 and .02 <= p.y <= .98 for p in views):
                break
            position = target + (position - target) * 1.15
        else:
            raise ValueError('Python camera framing could not fit focus geometry')
        _pv_key(camera, frame)
        previous_rotation = camera.rotation_euler.copy()
        previous_position, previous_target = position.copy(), target.copy()
        camera_samples.append({'frame': frame, 'focus': setup['focus'], 'framing': setup['framing']})
    actor_ids = {s['id'] for s in plan['subjects'] if s['kind'] != 'prop'}
    intervals = []
    for s in plan['scenes']:
        focus_ids = set(s['camera']['focus'])
        subjects = [rigs[key]['root'].name for key in sorted((actor_ids & focus_ids) or focus_ids)]
        intervals.append({'start': round(s['start'] * 24) + 1, 'end': round(s['end'] * 24), 'subjects': subjects})
    # Furniture/wall props must remain occluders, not be exempted as actor geometry.
    camera['previs_framing_subjects'] = json.dumps(sorted({key for s in intervals for key in s['subjects']}))
    camera['previs_focus_intervals'] = json.dumps(intervals)
    camera['previs_declarative_framing_checked'] = True
    _pv_linear([camera])
    return camera_samples


def build_declarative_scene(plan, duration, aspect):
    import bpy
    scene = bpy.context.scene
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    previs_reset_quality_issues()
    scene.frame_start, scene.frame_end, scene.render.fps = 1, int(duration * 24), 24
    scene.render.fps_base = 1
    aw, ah = (int(v) for v in aspect.split(':'))
    scene.render.resolution_x = 640 if aw >= ah else round(640 * aw / ah)
    scene.render.resolution_y = 640 if ah > aw else round(640 * ah / aw)
    scene.render.resolution_percentage = 100
    scene.world = bpy.data.worlds.new('SceneWorld')
    scene.world.color = plan['background']
    _scene_primitive('ENV_floor', 'box', (0, 0, -.12), (120, 120, .2), (.28, .3, .33))
    rigs = {}
    for s in plan['subjects']:
        if s['kind'] == 'human':
            rig = previs_humanoid(s['id'], (0, 0, 0), (*s['color'], 1))
        elif s['kind'] in ('quadruped', 'bird'):
            rig = _scene_animal(s)
        else:
            root = bpy.data.objects.new(s['id'], None)
            scene.collection.objects.link(root)
            body = _scene_primitive(s['id'] + '_mesh', s['shape'], (0, 0, .5), (1, 1, 1), s['color'], root)
            rig = {'root': root, 'parts': {'body': body}}
        rig['root']['previs_entity_id'] = s['id']
        rig['root']['previs_label'] = s['label']
        rig['root'].scale = s['size']
        rigs[s['id']] = rig
    for frame in range(1, scene.frame_end + 1):
        t = (frame - 1) / 24
        samples = {s['id']: sample_track(plan, s['id'], t) for s in plan['subjects']}
        # Resolve every root before contact targets; actor order cannot affect interaction.
        for s in plan['subjects']:
            rig, sample = rigs[s['id']], samples[s['id']]
            rig['root'].location = sample['position']
            rig['root'].rotation_euler = (0, 0, _scene_math.radians(sample['yaw']))
        bpy.context.view_layer.update()
        for s in plan['subjects']:
            rig, sample = rigs[s['id']], samples[s['id']]
            target = None
            if sample['target']:
                target_root = rigs[sample['target']]['root']
                from mathutils import Vector
                target = target_root.matrix_world @ Vector((0, 0, .8))
            if s['kind'] == 'human':
                _scene_apply_human(rig, sample, frame, target)
            else:
                root, parts = rig['root'], rig['parts']
                if sample['action'] in ('jump', 'fly'):
                    root.location.z += .4 * _scene_math.sin(_scene_math.pi * sample['phase'])
                if sample['action'] in ('fall', 'lie_down'):
                    root.rotation_euler.x = sample['phase'] * _scene_math.pi / 2
                gait = sample['action'] in ('walk', 'run', 'fly')
                for name, part in parts.items():
                    if 'front' in name or 'back' in name or 'wing' in name:
                        phase = (0 if name.startswith('L') else _scene_math.pi) + (0 if 'front' in name else _scene_math.pi)
                        part.rotation_euler.x = .5 * _scene_math.sin(sample['seconds'] * 10 + phase) if gait else 0
                        _pv_key(part, frame)
                _pv_key(root, frame)
    for rig in rigs.values():
        _pv_linear([rig['root'], *rig['parts'].values()])
    samples = _scene_build_camera(scene, rigs, plan)
    visibility = previs_validate_visibility({})
    scene.frame_set(1)
    # Exposed to the existing spatial manifest writer and downstream asset matching.
    globals()['subjects'] = [r['root'] for r in rigs.values()]
    scene['declarative_scene_version'] = 1
    return {'subjectCount': len(rigs), 'declarativeValidation': {
        'passed': bool(visibility.get('passed', True)), 'visibility': visibility,
        'framingChecked': True, 'frames': len(samples)},
        'sceneIntervals': [{'start': s['start'], 'end': s['end']} for s in plan['scenes']]}
