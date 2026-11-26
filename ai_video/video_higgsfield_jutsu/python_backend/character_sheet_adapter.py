"""Reconcile geometric layout hints with validated visual identity analysis.

Dividers partition pixels, not identities. Never fabricate people/crops from a
divider count or discard valid identity regions just to match that count.
"""


def compare_sheet_layout(analysis, layout_hints):
    """Diagnostics only: call after sheet indices/regions pass validation."""
    hints = {hint['imageIndex']: hint for hint in layout_hints}
    comparisons = []
    for sheet in analysis['sheets']:
        hint = hints.get(sheet['imageIndex'])
        if hint is None:
            continue
        members = sheet['members']
        # Empty members also denotes an unsegmented group photo in the wire
        # contract, so do not claim that Python measured exactly one person.
        count = len(members) if members else None
        comparisons.append({
            'imageIndex': sheet['imageIndex'],
            'detectedRegionCount': hint['count'],
            'identityCount': count,
            'interpretation': 'distinct_identity_regions' if members else 'single_or_unsegmented',
            'status': 'matched' if count == hint['count'] else 'layout_hint_difference',
            'decision': 'keep_validated_identity_regions' if members else 'keep_whole_image',
            'reason': 'Divider regions may contain different views or details of the same identity.',
        })
    return comparisons
