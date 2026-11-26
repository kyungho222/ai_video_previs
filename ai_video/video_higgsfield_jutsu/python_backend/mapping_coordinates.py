"""Explicit provider coordinate conversion; no guessed units or axis order."""
from copy import deepcopy

def normalize_model_mapping(value, *, error_type=ValueError):
    """Convert the explicit model yxyx/1000 contract, never guess unknown units."""
    value = deepcopy(value)
    if isinstance(value, dict) and 'cameraSummary' not in value and 'entities' not in value:
        properties = value.get('properties')
        if (isinstance(properties, dict) and isinstance(properties.get('cameraSummary'), str)
                and isinstance(properties.get('entities'), list)):
            value = properties
    for entity in value.get('entities', []):
        for sample in entity.get('frames', []):
            box = sample.get('bbox')
            if (not isinstance(box,list) or len(box)!=4
                    or any(type(x) is not int or not 0<=x<=1000 for x in box)):
                raise error_type('bbox must follow the declared integer yxyx_1000 contract')
            y1,x1,y2,x2 = box
            if sample.get('visible') and (x1>=x2 or y1>=y2):
                raise error_type('visible bbox requires ordered nonempty yxyx_1000 bounds')
            sample['bbox'] = [x1/1000,y1/1000,x2/1000,y2/1000]
    return value

