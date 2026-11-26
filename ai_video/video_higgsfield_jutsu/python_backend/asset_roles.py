"""Four canonical asset types shared by upload, analysis and generation."""
IMAGE_ROLES={'background':'background and environment', 'character':'character appearance',
             'object':'object and prop appearance'}
ASSET_TYPES=(*IMAGE_ROLES,'audio')


def canonical_asset_role(role):
    if not isinstance(role,str):
        return None
    return {'other':'object','prop':'object','environment':'background'}.get(role,role)


def partition_assets(assets):
    result={role:[] for role in ASSET_TYPES}
    for asset in assets:
        role=canonical_asset_role(asset.get('role'))
        if role not in result:
            raise ValueError('에셋 유형은 배경, 캐릭터, 사물, 음원 중 하나여야 합니다.')
        result[role].append({**asset,'role':role})
    return result
