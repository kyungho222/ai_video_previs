"""Permanent media have a JSON/SQL null expiry; old dated rows remain explicit."""
import time


def is_expired(expires_at, *, now=None):
    return expires_at is not None and (time.time() if now is None else now) >= expires_at
