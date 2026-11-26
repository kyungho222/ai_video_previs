"""Retain quality warnings while permitting existing generated guide images."""


def guide_decision(guide, *, force=False):
    if guide.get('status') == 'verified':
        return {'action': 'allow', 'reason': 'verified'}
    verification = guide.get('verification') or {}
    checks = ('castCountPreserved', 'assetIdentityPreserved', 'cameraCompositionPreserved',
              'sceneStatePreserved', 'locationContinuityPreserved', 'proxyShapesRemoved')
    failed = [name for name in checks if verification.get(name) is False]
    reason = ', '.join(failed) or guide.get('verificationIssue') or 'verification_unconfirmed'
    usable = bool(guide.get('name')) and guide.get('status') in ('generated', 'invalid', 'excluded', 'verified')
    return {'action': 'override' if usable else 'block', 'reason': str(reason)[:600]}
