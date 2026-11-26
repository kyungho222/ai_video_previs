"""Model-independent inventory and evidence-based mapping status."""
from copy import deepcopy
import hashlib
import json
import math


def attach_environment_references(mapping, assets):
    """Background registration never depends on a guessed foreground bounding box."""
    result=deepcopy(mapping)
    if not isinstance(result,dict) or not isinstance(result.get('entities'),list):
        return result
    covered={i for e in result['entities'] if isinstance(e,dict) and isinstance(e.get('assetIndices'),list)
             for i in e['assetIndices'] if type(i) is int}
    refs=[]
    for index,asset in enumerate(assets,1):
        if index in covered or asset.get('role') not in ('background','environment'):
            continue
        refs.append({'assetIndex':index,'assetId':str(asset.get('id') or asset.get('assetId') or f'input-{index}'),
                     'sourceIndex':asset.get('sourceIndex',index),'role':asset['role'],
                     'targetType':'environment','spatialStatus':'unresolved','bbox':None,'visible':None})
    result['environmentReferences']=refs
    return result


def build_asset_inventory(assets):
    from .asset_roles import canonical_asset_role
    inventory = []
    roles = {'character': 'character', 'object': 'object', 'prop': 'object',
             'background': 'background', 'environment': 'environment', 'other': 'other'}
    for index, asset in enumerate(assets, 1):
        original_id = asset.get('id') or asset.get('assetId') or f'input-{index}'
        part = {'sourceIndex': asset.get('sourceIndex'), 'bbox': asset.get('bbox'), 'index': index}
        suffix = hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()[:12]
        category = roles.get(canonical_asset_role(asset.get('role')), 'unknown')
        inventory.append({'index': index, 'inventoryId': str(original_id) + ':' + suffix,
                          'assetId': str(original_id), 'sourceIndex': asset.get('sourceIndex', index),
                          'category': category,
                          'targetType': 'environment' if category in ('background', 'environment') else 'object' if category in ('character', 'object') else 'unknown',
                          'label': asset.get('label', ''), 'sourceCrop': deepcopy(asset.get('bbox')),
                          'explicitObjectId': asset.get('sourceObjectId'),
                          'registrationStatus': 'registered'})
    return inventory


def _sample_valid(sample):
    if not isinstance(sample, dict) or type(sample.get('visible')) is not bool:
        return False
    box = sample.get('bbox')
    if not isinstance(box, list) or len(box) != 4 or any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in box):
        return False
    return (box[2] > box[0] and box[3] > box[1]) if sample['visible'] else box == [0, 0, 0, 0]


def assess_asset_inventory(inventory, mapping, frames, scene_manifest=None):
    entities = mapping.get('entities', []) if isinstance(mapping, dict) else []
    entities = entities if isinstance(entities, list) else []
    objects = (scene_manifest or {}).get('objects', [])
    known = {obj.get('id') for obj in objects if isinstance(obj, dict) and isinstance(obj.get('id'), str)}
    output = []
    for asset in inventory:
        row = deepcopy(asset)
        matches = [e for e in entities if isinstance(e, dict) and isinstance(e.get('assetIndices'), list)
                   and asset['index'] in e['assetIndices']]
        row.update(linkStatus='unresolved', linkSource=None, entityId=None, objectIds=[],
                   nativeObjectStatus='unresolved', frames=[])
        environment_ref=next((r for r in mapping.get('environmentReferences',[]) if isinstance(r,dict)
                              and r.get('assetIndex')==asset['index']),None) if isinstance(mapping,dict) else None
        if len(matches) > 1:
            row['linkStatus'] = 'conflict'
        elif len(matches) == 1 and isinstance(matches[0].get('id'), str) and matches[0]['id']:
            entity = matches[0]
            source_ids = entity.get('sourceObjectIds')
            if isinstance(source_ids, list) and any(i not in known for i in source_ids):
                row['linkStatus'] = 'conflict'
            else:
                confidence = entity.get('confidence')
                observations = entity.get('frames') if isinstance(entity.get('frames'), list) else []
                uncertain = (type(confidence) not in (int, float) or not math.isfinite(confidence)
                             or not 0 <= confidence <= 1 or
                             (confidence < .65 and any(isinstance(v, dict) and v.get('visible') is True for v in observations)))
                row.update(linkStatus='unresolved' if uncertain else 'linked', linkSource='confirmed_binding' if entity.get('frameSource')=='blender' else 'model', entityId=entity['id'],
                           objectIds=deepcopy(source_ids or []))
        elif environment_ref and asset['category'] in ('background','environment'):
            row.update(linkStatus='linked',linkSource='registered_environment_reference',entityId='scene_environment')
        elif asset.get('explicitObjectId') in known:
            row.update(linkStatus='linked', linkSource='explicit_native_id', objectIds=[asset['explicitObjectId']])
        elif (not matches and asset['category'] in ('background', 'environment')
              and sum(item['category'] in ('background', 'environment') for item in inventory) == 1):
            # Role identifies the scene environment, not its screen region.
            row.update(linkStatus='linked', linkSource='unique_background_role', entityId='scene_environment')
        if row['objectIds'] and row['linkStatus'] == 'linked':
            row['nativeObjectStatus'] = 'linked'
        entity = matches[0] if len(matches) == 1 and row['linkStatus'] == 'linked' else {}
        samples = entity.get('frames') if isinstance(entity.get('frames'), list) else []
        for frame in frames:
            index = frame.get('index')
            observations = [s for s in samples if isinstance(s, dict) and s.get('index') == index]
            valid = len(observations) == 1 and _sample_valid(observations[0])
            frame_result = {'index': index, 'visible': observations[0]['visible'] if valid else None,
                            'bbox': deepcopy(observations[0]['bbox']) if valid else None,
                            'source': ('blender' if entity.get('frameSource')=='blender' else 'model') if valid else None}
            time = frame.get('time')
            native_frames = (scene_manifest or {}).get('frames', [])
            matching = [f for f in native_frames if isinstance(f, dict) and type(time) in (int, float)
                        and type(f.get('time')) in (int, float) and abs(f['time'] - time) < 1e-5]
            if len(matching) == 1 and len(row['objectIds']) == 1:
                native = matching[0]
                subjects = native.get('subjects') if isinstance(native.get('subjects'), list) else []
                objects = [o for o in subjects if isinstance(o, dict) and o.get('id') == row['objectIds'][0]]
                if len(objects) == 1:
                    frame_result['native'] = {'objectLocation': deepcopy(objects[0].get('location')),
                                              'camera': deepcopy(native.get('camera'))}
            row['frames'].append(frame_result)
        row['analysisStatus'] = ('complete' if row['frames'] and all(f['visible'] is not None for f in row['frames'])
                                 and row['linkStatus'] == 'linked' else 'unresolved')
        output.append(row)
    return output


def complement_mapping_from_native(mapping, assets, frames, scene_manifest):
    """Add only explicit identity and measured frame data; never synthesize visibility."""
    result = deepcopy(mapping)
    if not isinstance(result, dict) or not isinstance(result.get('entities'), list):
        return result, []
    objects = (scene_manifest or {}).get('objects', [])
    native_frames = (scene_manifest or {}).get('frames', [])
    known = {o.get('id') for o in objects if isinstance(o, dict) and isinstance(o.get('id'), str)}
    covered = {i for e in result['entities'] if isinstance(e, dict) and isinstance(e.get('assetIndices'), list)
               for i in e['assetIndices'] if type(i) is int}
    used_ids = {i for e in result['entities'] if isinstance(e, dict) and isinstance(e.get('sourceObjectIds'), list)
                for i in e['sourceObjectIds'] if isinstance(i, str)}
    changes = []
    for index, asset in enumerate(assets, 1):
        source = asset.get('sourceObjectId')
        appearance, label = asset.get('appearance'), asset.get('label')
        if (index in covered or source not in known or source in used_ids
                or sum(a.get('sourceObjectId') == source for a in assets) != 1
                or not isinstance(appearance, str) or not appearance.strip()
                or not isinstance(label, str) or not label.strip()):
            continue
        samples = []
        for frame in frames:
            t = frame.get('time')
            matching = [f for f in native_frames if isinstance(f, dict) and type(t) in (int, float)
                        and type(f.get('time')) in (int, float) and abs(f['time'] - t) < 1e-5]
            if len(matching) != 1:
                break
            entries = matching[0].get('subjects')
            entries = [v for v in entries if isinstance(v, dict) and v.get('id') == source] if isinstance(entries, list) else []
            if len(entries) != 1 or not _sample_valid(entries[0]):
                break
            samples.append({'index': frame['index'], 'visible': entries[0]['visible'],
                            'bbox': deepcopy(entries[0]['bbox'])})
        if len(samples) != len(frames) or not samples:
            continue
        entity_id = 'native_asset_' + str(index)
        if any(e.get('id') == entity_id for e in result['entities'] if isinstance(e, dict)):
            continue
        result['entities'].append({'id': entity_id, 'label': label, 'appearance': appearance,
                                   'assetIndices': [index], 'sourceObjectIds': [source],
                                   'confidence': 1.0, 'frames': samples})
        changes.append({'assetIndex': index, 'sourceObjectId': source, 'reason': 'explicit_native_projection'})
        used_ids.add(source)
    return result, changes
