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
    for name in ('Head', 'Nose'):
        parts[name].location.z = 1.76 - pose['crouch']
        _pv_key(parts[name], frame)
    _pv_key(parts['Torso'], frame)
    _pv_key(parts['Pelvis'], frame)
    pointing = None
    if target is not None and (sample.get('gesture_action') or sample['action']) == 'point':
        import bpy
        bpy.context.view_layer.update()
        local = root.matrix_world.inverted() @ Vector(target)
        pointing = pointing_goal(list(local), {side:pose['joints'][side+'_Shoulder'] for side in ('L','R')})
    for side, sign in (('L', -1), ('R', 1)):
        j = pose['joints']
        hip, foot = Vector(j[side + '_Hip']), Vector(j[side + '_Foot'])
        shoulder, hand = Vector(j[side + '_Shoulder']), Vector(j[side + '_Hand'])
        if pointing:
            hand = Vector((sign * .32, 0, .94 - pose['crouch']))
            if side == pointing['side']:
                blend = min(1, sample.get('gesture_phase', sample['phase']) * 3)
                hand = hand.lerp(Vector(pointing['hand']), blend)
        elif side == 'R' and target is not None and (sample.get('gesture_action') or sample['action']) in ('reach', 'hold', 'slap'):
            # World target is converted into this actor's coordinates, including actor scale.
            local = root.matrix_world.inverted() @ Vector(target)
            blend = min(1, sample.get('gesture_phase', sample['phase']) * 3)
            hand = hand.lerp(local, blend)
        knee, foot = (Vector(p) for p in _pv_two_bone(hip, foot, .5, .5, (0, 1, 0)))
        elbow, hand = (Vector(p) for p in _pv_two_bone(shoulder, hand, .30, .28, (sign, .2, -.2)))
        if pointing and side == pointing['side']:
            desired, actual = local - shoulder, hand - shoulder
            cosine = max(-1., min(1., desired.dot(actual) / max(1e-9, desired.length * actual.length)))
            rig.setdefault('pointingDiagnostics', []).append({
                'frame':frame, 'side':side, 'targetWorld':list(target),
                'handWorld':list(root.matrix_world @ hand), 'directionErrorDegrees':_scene_math.degrees(_scene_math.acos(cosine)),
                'contactRequired':False, 'targetDistance':pointing['targetDistance'],
                'bodyAvoidanceApplied':pointing['avoidanceApplied'],
                'targetResolution':sample.get('targetResolution', 'camera_position' if action_target(sample) == 'camera' else 'unspecified'),
                'crossesBodyMidline':shoulder.x * hand.x < 0})
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


def _scene_framing_meshes(meshes, framing):
    """Prefer actual head geometry for close shots; fall back for other models."""
    if framing == 'close':
        heads = [mesh for mesh in meshes if mesh.name.lower().split('_')[-1] == 'head']
        if heads:
            return heads
    return meshes


def _scene_projection_measurements(points, framing):
    minimum = {'close': .50, 'medium': .10, 'full': .08, 'wide': .03}.get(framing, .08)
    result = {'bounds': None, 'width': None, 'height': None, 'minimumSize': minimum,
              'frameMargin': .02, 'behindCameraPoints': sum(p[2] <= 0 for p in points)}
    if points:
        left, right = min(p[0] for p in points), max(p[0] for p in points)
        bottom, top = min(p[1] for p in points), max(p[1] for p in points)
        result.update(bounds={'left': round(left, 5), 'right': round(right, 5),
                              'bottom': round(bottom, 5), 'top': round(top, 5)},
                      width=round(right - left, 5), height=round(top - bottom, 5))
    return result


def _scene_projection_issues(points, framing):
    """Check projected bounds; warnings do not override explicit camera direction."""
    if not points:
        return ['missing_geometry']
    if any(p[2] <= 0 or not (.02 <= p[0] <= .98 and .02 <= p[1] <= .98) for p in points):
        return ['outside_frame']
    width = max(p[0] for p in points) - min(p[0] for p in points)
    height = max(p[1] for p in points) - min(p[1] for p in points)
    minimum = {'close': .50, 'medium': .10, 'full': .08, 'wide': .03}.get(framing, .08)
    return ['too_small'] if max(width, height) < minimum else []


def _scene_fit_points(actual_points, transition_points):
    """Fit the visible subject as well as the interpolated transition target."""
    return actual_points if actual_points is transition_points else list(actual_points) + list(transition_points)


def _scene_bounds(roots, scene, framing='full'):
    import bpy
    from mathutils import Vector
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for root in roots:
        meshes = [o for o in [root, *root.children_recursive] if o.type == 'MESH' and not o.hide_render]
        # Context props need their whole display surface, not a human upper-body crop.
        object_framing = 'full' if root.get('previs_kind') == 'prop' else framing
        selected = _scene_framing_meshes(meshes, object_framing)
        head_based = object_framing == 'close' and selected is not meshes
        current = [o.evaluated_get(depsgraph).matrix_world @ Vector(c) for o in selected for c in o.bound_box]
        if not current:
            raise ValueError('visible subject geometry is missing')
        lo = Vector(tuple(min(p[i] for p in current) for i in range(3)))
        hi = Vector(tuple(max(p[i] for p in current) for i in range(3)))
        # Close/medium target actual upper geometry, never the subject's ground-level origin.
        if object_framing in ('medium', 'close') and not head_based:
            fraction = .55 if object_framing == 'medium' else .24
            lo.z = hi.z - (hi.z - lo.z) * fraction
            if object_framing == 'close':
                center_x = (lo.x + hi.x) / 2
                half = max(.12, (hi.z - lo.z) * .7)
                lo.x, hi.x = center_x - half, center_x + half
        points.extend(Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z))
    return points


def _scene_floor_safe_position(position, floor_z, clearance=.1):
    position = list(position)
    minimum = floor_z + clearance
    if position[2] < minimum:
        position[2] = minimum
        return position, True
    return position, False


def _scene_support_floor(focus_bases, surface_bounds, default=-.02, tolerance=.05):
    """Measured support beneath feet, not the tallest object in the scene."""
    height = default
    for base in focus_bases:
        for low, high in surface_bounds:
            if (low[0] - .01 <= base[0] <= high[0] + .01
                    and low[1] - .01 <= base[1] <= high[1] + .01
                    and abs(base[2] - high[2]) <= tolerance):
                height = max(height, high[2])
    return height


def _scene_focus_support_height(scene, rigs, plan, focus):
    bases, surfaces = [], []
    for subject in plan['subjects']:
        if subject['kind'] == 'prop':
            root = rigs[subject['id']]['root']
            points = _scene_bounds([root], scene, 'full')
            if points:
                surfaces.append(([min(p[i] for p in points) for i in range(3)],
                                 [max(p[i] for p in points) for i in range(3)]))
    if not surfaces:
        return -.02
    for subject in plan['subjects']:
        if subject['id'] in focus and subject['kind'] != 'prop':
            bases.append(list(rigs[subject['id']]['root'].matrix_world.translation))
    return _scene_support_floor(bases, surfaces)


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
    position_velocity = target_velocity = [0., 0., 0.]
    motion_warnings = []
    floor_adjusted_shots = set()
    locked = {}
    camera_samples = []
    projection_warnings = []
    for frame in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(frame)
        time = (frame - 1) / 24
        index = next((i for i, s in enumerate(setups) if time < s['end']), len(setups) - 1)
        beat = setups[index]
        setup = beat['camera']
        roots = [rigs[key]['root'] for key in setup['focus']]
        points = _scene_bounds(roots, scene, setup['framing'])
        actual_points = points
        # The region of interest itself must change continuously. Switching from
        # head-only to full-body bounds in one frame defeats every pose smoother
        # because the mandatory framing refit immediately moves the camera back.
        elapsed = time - beat['start']
        is_cut, blend = camera_transition(index, elapsed, beat['end'] - beat['start'], setup.get('transition', 'continuous'))
        is_cut = index > 0 and setup.get('transition') == 'cut' and camera_samples[-1]['shot'] != index
        if is_cut:
            previous_position = previous_target = previous_rotation = None
            position_velocity = target_velocity = [0., 0., 0.]
        if index and blend < 1:
            previous_setup = setups[index - 1]['camera']
            previous_roots = [rigs[key]['root'] for key in previous_setup['focus']]
            previous_points = _scene_bounds(previous_roots, scene, previous_setup['framing'])
            if len(previous_points) == len(points):
                points = [a.lerp(b, blend) for a, b in zip(previous_points, points)]
        u = max(0, min(1, (time - beat['start']) / (beat['end'] - beat['start'])))
        if setup['movement'] in ('locked', 'pan'):
            lock_key = json.dumps(setup, sort_keys=True) + (':' + str(index) if setup.get('transition') == 'cut' else '')
            if lock_key not in locked:
                # Fit the entire interval's moving envelope once; a fixed shot stays fixed.
                # Identical locked setups across scenario cards are one continuous shot.
                envelope = []
                intervals = [beat] if setup.get('transition') == 'cut' else [s for s in setups if s['camera'] == setup]
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
        # Scenario cards are not cuts. Limit motion throughout the entire path,
        # including changes of framing, not just the first .3 seconds of a card.
        if previous_position is not None:
            speed_limit = max(2., (position - target).length * 2.)
            position, position_velocity = camera_step(previous_position, position, position_velocity,
                                                       1 / 24, speed_limit, speed_limit * 4)
            target, target_velocity = camera_step(previous_target, target, target_velocity,
                                                   1 / 24, 3., 12.)
            position, target = Vector(position), Vector(target)
        smoothed_position = position.copy()
        # Fit the actual new subject as well as the interpolated region. Fitting
        # only the latter can hide clipping during an otherwise smooth transition.
        fit_points = _scene_fit_points(actual_points, points)
        support_floor = _scene_focus_support_height(scene, rigs, plan, setup['focus'])
        # Ensure all intended focus geometry is inside frame even through transitions.
        for _ in range(16):
            # ENV_floor has its top at z=-.02. Keep the camera above it even
            # when low elevation or the framing refit moves it below ground.
            safe_position, floor_adjusted = _scene_floor_safe_position(position, support_floor)
            position = Vector(safe_position)
            if floor_adjusted and index not in floor_adjusted_shots:
                motion_warnings.append({'frame': frame, 'reason': 'floor_clearance', 'shot': index,
                                        'supportTop': support_floor, 'minimumCameraHeight': support_floor + .1})
                floor_adjusted_shots.add(index)
            rotation = (target - position).to_track_quat('-Z', 'Y')
            camera.location = position
            camera.rotation_euler = rotation.to_euler('XYZ', previous_rotation) if previous_rotation is not None else rotation.to_euler('XYZ')
            bpy.context.view_layer.update()
            views = [world_to_camera_view(scene, camera, point) for point in fit_points]
            if all(p.z > 0 and .02 <= p.x <= .98 and .02 <= p.y <= .98 for p in views):
                break
            # Compute the required fit distance instead of jumping back 15%.
            # Coarse multiplicative steps caused visible single-frame pops.
            outward = (position - target).normalized()
            inverse = rotation.inverted()
            corners = camera.data.view_frame(scene=scene)
            tan_x = max(abs(p.x / p.z) for p in corners)
            tan_y = max(abs(p.y / p.z) for p in corners)
            distance = (position - target).length
            for point in fit_points:
                local = inverse @ (point - target)
                distance = max(distance, local.z + abs(local.x) / (tan_x * .959),
                               local.z + abs(local.y) / (tan_y * .959), local.z + .02)
            position = target + outward * distance
        else:
            motion_warnings.append({'frame': frame, 'shot': index, 'reason': 'framing_fit_incomplete'})
            position = Vector(_scene_floor_safe_position(position, support_floor)[0])
            camera.location = position
            camera.rotation_euler = (target - position).to_track_quat('-Z', 'Y').to_euler('XYZ')
            bpy.context.view_layer.update()
        # Framing takes precedence over motion limits. Report exceptional refits
        # rather than claiming continuity when geometry forced a camera jump.
        actual_views = [world_to_camera_view(scene, camera, point) for point in actual_points]
        projection_issues = _scene_projection_issues([(p.x, p.y, p.z) for p in actual_views], setup['framing'])
        if projection_issues:
            projection_warnings.append({'frame': frame, 'issues': projection_issues, 'focus': list(setup['focus']),
                'framing': setup['framing'],
                'measurements': _scene_projection_measurements([(p.x, p.y, p.z) for p in actual_views], setup['framing']),
                'cameraPosition': [round(v, 5) for v in position],
                'cameraTarget': [round(v, 5) for v in target]})
        refit_distance = (position - smoothed_position).length
        if refit_distance > .05:
            motion_warnings.append({'frame': frame, 'reason': 'framing_refit',
                                    'distance': round(refit_distance, 4)})
        if previous_position is not None:
            position_velocity = list((position - previous_position) * 24)
        _pv_key(camera, frame)
        previous_rotation = camera.rotation_euler.copy()
        previous_position, previous_target = position.copy(), target.copy()
        camera_samples.append({'frame': frame, 'shot': index, 'movement': setup['movement'],
                               'transitioning': blend < 1 or (setup.get('transition', 'continuous') != 'cut' and elapsed < .9), 'focus': setup['focus'], 'framing': setup['framing'],
                               'position': list(position), 'target': list(target), 'supportTop': support_floor})
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
    camera['previs_motion_warnings'] = json.dumps(motion_warnings)
    camera['previs_projection_warnings'] = json.dumps(projection_warnings)
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
            definitions = scene_prop_parts(s['label'])
            if definitions is None:
                definitions = [(s['shape'], (0, 0, .5), (1, 1, 1), 1)]
            parts = {}
            for index, (shape, position, size, shade) in enumerate(definitions):
                color = tuple(max(0, min(1, channel * shade)) for channel in s['color'])
                parts['part_' + str(index)] = _scene_primitive(
                    s['id'] + '_part_' + str(index), shape, position, size, color, root)
            rig = {'root': root, 'parts': parts}
        rig['root']['previs_entity_id'] = s['id']
        rig['root']['previs_kind'] = s['kind']
        rig['root']['previs_label'] = s['label']
        rig['root'].scale = s['size']
        rig['root'].location = s['position']
        rigs[s['id']] = rig
    bpy.context.view_layer.update()
    def world_positions():
        bpy.context.view_layer.update()
        return {key: [float(v) for v in rig['root'].matrix_world.translation] for key, rig in rigs.items()}
    created_positions = world_positions()
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
            target_id = action_target(sample)
            if target_id and target_id != 'camera':
                target_root = rigs[target_id]['root']
                from mathutils import Vector
                target = target_root.matrix_world @ Vector((0, 0, .8))
                if (sample.get('gesture_action') or sample['action']) == 'point':
                    bounds = _scene_bounds([target_root], scene, 'full')
                    target = Vector(tuple((min(p[i] for p in bounds) + max(p[i] for p in bounds)) / 2 for i in range(3)))
                    sample['targetResolution'] = 'object_center'
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
    scene.frame_set(1)
    motion_start = world_positions()
    scene.frame_set(scene.frame_end)
    motion_end = world_positions()
    samples = _scene_build_camera(scene, rigs, plan)
    facing_diagnostics = []
    facing_tracks = [t for t in plan['tracks'] if t.get('faceCamera') and t.get('action') == 'turn']
    if facing_tracks:
        from mathutils import Vector
        from bpy_extras.object_utils import world_to_camera_view
        updated_projection = []
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            time = (frame - 1) / 24
            for track in facing_tracks:
                if not track['start'] <= time < track['end']:
                    continue
                root = rigs[track['subject']]['root']
                before = _scene_math.degrees(root.rotation_euler.z)
                u = min(1., (time - track['start']) / max(1 / 24, track['end'] - track['start'] - 1 / 24))
                weight = u * u * (3 - 2 * u)
                after = face_camera_yaw(root.matrix_world.translation, scene.camera.matrix_world.translation, before, weight)
                root.rotation_euler.z = _scene_math.radians(after)
                root.keyframe_insert(data_path='rotation_euler', frame=frame)
                desired = face_camera_yaw(root.matrix_world.translation, scene.camera.matrix_world.translation, after)
                facing_diagnostics.append({'frame': frame, 'subject': track['subject'], 'beforeYaw': before,
                    'afterYaw': after, 'remainingAngle': abs((desired - after + 180) % 360 - 180), 'weight': weight})
            bpy.context.view_layer.update()
            # Facing changes evaluated geometry: do not reuse pre-correction
            # projection evidence as if it measured the final actor orientation.
            shot = samples[frame - scene.frame_start]
            points = _scene_bounds([rigs[key]['root'] for key in shot['focus']], scene, shot['framing'])
            views = [world_to_camera_view(scene, scene.camera, p) for p in points]
            values = [(p.x, p.y, p.z) for p in views]
            issues = _scene_projection_issues(values, shot['framing'])
            if issues:
                updated_projection.append({'frame': frame, 'issues': issues, 'focus': shot['focus'],
                    'framing': shot['framing'], 'measurements': _scene_projection_measurements(values, shot['framing']),
                    'cameraPosition': shot['position'], 'cameraTarget': shot['target'], 'stage': 'after_facing'})
        scene.camera['previs_projection_warnings'] = json.dumps(updated_projection)
        for ident in {t['subject'] for t in facing_tracks}:
            _pv_linear([rigs[ident]['root']])
    # Camera is keyed after character roots. Apply camera-targeted gestures in
    # a second pass against the evaluated camera at each frame.
    camera_targets = {t['subject'] for t in plan['tracks'] if t.get('target') == 'camera' and t.get('action') in ('point', 'reach')}
    if camera_targets:
        for frame in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            for ident in camera_targets:
                sample = sample_track(plan, ident, (frame - 1) / 24)
                if action_target(sample) == 'camera' and (sample.get('gesture_action') or sample.get('action')) in ('point', 'reach'):
                    sample['yaw'] = _scene_math.degrees(rigs[ident]['root'].rotation_euler.z)
                    _scene_apply_human(rigs[ident], sample, frame, scene.camera.matrix_world.translation.copy())
        for ident in camera_targets:
            _pv_linear([rigs[ident]['root'], *rigs[ident]['parts'].values()])
    visibility = previs_validate_visibility({})
    scene.frame_set(1)
    final_positions = world_positions()
    object_positions = {key: {'rootName': rig['root'].name, 'created': created_positions[key],
                              'afterMotionApplied': motion_start[key], 'motionEnd': motion_end[key],
                              'final': final_positions[key], 'sampleFrame': 1, 'motionEndFrame': scene.frame_end}
                        for key, rig in rigs.items()}
    # Exposed to the existing spatial manifest writer and downstream asset matching.
    globals()['subjects'] = [r['root'] for r in rigs.values()]
    scene['declarative_scene_version'] = 1
    projection = json.loads(scene.camera.get('previs_projection_warnings', '[]'))
    quality = camera_quality(samples, projection)
    return {'subjectCount': len(rigs), 'declarativeValidation': {
        'passed': bool(visibility.get('passed', True)) and quality['status'] == 'matched',
        'visibility': visibility, 'camera': {'passed': quality['status'] == 'matched', 'issues': quality['issues']},
        'framingChecked': True, 'frames': len(samples)},
        'serverCorrection': {'objectPositions': object_positions, 'cameraQuality': quality, 'cameraMotionWarnings': json.loads(scene.camera['previs_motion_warnings']),
                             'facingDiagnostics': facing_diagnostics,
                             'pointingDiagnostics': {key:rig.get('pointingDiagnostics', []) for key,rig in rigs.items() if rig.get('pointingDiagnostics')},
                             'cameraSamples': samples,
                             'projectionWarnings': json.loads(scene.camera.get('previs_projection_warnings', '[]'))},
        'sceneIntervals': [{'start': s['start'], 'end': s['end']} for s in plan['scenes']]}
