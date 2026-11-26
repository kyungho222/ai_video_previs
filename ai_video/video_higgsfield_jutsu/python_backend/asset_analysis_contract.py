"""Python-owned asset facts and model-independent per-asset analysis slots."""
from copy import deepcopy
from pathlib import Path
import math

from PIL import Image, ImageOps

from .asset_mapping_inventory import build_asset_inventory


def build_asset_analysis_contract(assets, frames, sheet_refs=(), layout_count=0):
    inventory = build_asset_inventory(assets)
    refs = {ref['assetIndex']: ref for ref in sheet_refs or ()}
    result = {}
    for item, asset in zip(inventory, assets):
        index = item['index']
        row = {key: deepcopy(item[key]) for key in
               ('assetId', 'inventoryId', 'sourceIndex', 'sourceCrop', 'registrationStatus')}
        row.update(assetIndex=index, declaredRole=asset.get('role'),
                   suppliedLabel=asset.get('label', ''),
                   fileName=Path(str(asset.get('name') or asset.get('path', ''))).name,
                   requiredFrameIndices=[frame['index'] for frame in frames],
                   declaredSourceObjectId=asset.get('sourceObjectId'),
                   requiredAnalysis=['id', 'label', 'appearance', 'sourceObjectIds', 'confidence', 'frames'])
        size = None
        try:
            with Image.open(asset['path']) as source:
                row['imageFormat'] = source.format
                size = list(ImageOps.exif_transpose(source).size)
        except (KeyError, OSError, ValueError):
            # Missing measurements never become guessed identity or pixel facts.
            if all(type(asset.get(k)) is int and asset[k] > 0 for k in ('width', 'height')):
                size = [asset['width'], asset['height']]
        if size:
            row.update(pixelSize=size, aspectRatio=round(size[0]/size[1], 6),
                       orientation='landscape' if size[0] > size[1] else 'portrait' if size[1] > size[0] else 'square')
        ref = refs.get(index)
        row['referenceImageNumber'] = layout_count + (ref['sheetIndex'] if ref else index)
        if ref:
            row.update(referenceRegion=deepcopy(ref['bbox']),
                       referenceRegionConvention='normalized_xyxy',
                       renderedSize=deepcopy(ref.get('renderedSize')))
        result['asset_' + str(index)] = row
    return result


def asset_mapping_schema(mapping_schema, indices):
    entity = deepcopy(mapping_schema['properties']['entities']['items'])
    entity['properties'].pop('assetIndices')
    entity['required'].remove('assetIndices')
    properties = {'asset_' + str(i): deepcopy(entity) for i in indices}
    return {'type': 'object', 'properties': {
        'cameraSummary': deepcopy(mapping_schema['properties']['cameraSummary']),
        'assetMappings': {'type': 'object', 'properties': properties,
                          'required': list(properties), 'additionalProperties': False}},
        'required': ['cameraSummary', 'assetMappings'], 'additionalProperties': False}


def normalize_asset_mapping(value, indices):
    """Assign Python indices; retain incomplete responses for missing-only repair."""
    if not isinstance(value, dict) or 'assetMappings' not in value:
        return deepcopy(value)  # Compatibility with earlier model response shapes.
    slots = value['assetMappings']
    if not isinstance(slots, dict):
        raise ValueError('assetMappings must contain the required per-asset analysis objects')
    allowed = {'asset_' + str(i): i for i in indices}
    unexpected = sorted(set(slots) - set(allowed))
    if unexpected:
        raise ValueError('Unexpected asset analysis keys: ' + ', '.join(unexpected))
    entities, by_id = [], {}
    for key, index in allowed.items():
        if key not in slots:
            continue  # Not an offscreen observation; coverage validation reports this omission.
        entry = deepcopy(slots[key])
        if not isinstance(entry, dict):
            raise ValueError('Missing actual asset analysis: ' + key)
        if 'assetIndices' in entry:
            raise ValueError('assetIndices is owned by Python, not by asset analysis: ' + key)
        entry['assetIndices'] = [index]
        identifier = entry.get('id')
        previous = by_id.get(identifier) if isinstance(identifier, str) and identifier else None
        if previous is not None:
            # Same-identity views may share an entity only with matching spatial evidence.
            if any(previous.get(k) != entry.get(k) for k in ('frames', 'sourceObjectIds')):
                raise ValueError('Conflicting observations for shared asset entity: ' + identifier)
            previous['assetIndices'].append(index)
            for field in ('label', 'appearance'):
                if previous.get(field) != entry.get(field):
                    if not isinstance(previous.get(field), str) or not isinstance(entry.get(field), str):
                        raise ValueError('Invalid analysis for shared asset entity: ' + identifier)
                    previous[field] += '; ' + entry[field]
            confidence = entry.get('confidence')
            if any(type(c) not in (int, float) or not math.isfinite(c) or not 0 <= c <= 1
                   for c in (confidence, previous.get('confidence'))):
                raise ValueError('Invalid confidence for shared asset entity: ' + identifier)
            previous['confidence'] = min(previous['confidence'], confidence)
        else:
            entities.append(entry)
            if isinstance(identifier, str) and identifier:
                by_id[identifier] = entry
    result = {k: deepcopy(v) for k, v in value.items() if k not in ('assetMappings', 'entities')}
    if 'entities' in value and value['entities'] != entities:
        raise ValueError('Conflicting assetMappings and entities response lists')
    result['entities'] = entities
    return result
