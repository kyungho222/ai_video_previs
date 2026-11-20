"""Jutsu authentication API and its package-owned persistent storage."""

import hashlib
from pathlib import Path

from .jutsu_auth import JutsuPythonClient, OAuthStorage

__all__ = ["JutsuPythonClient", "OAuthStorage", "auth_database_path", "create_client"]


def auth_database_path(redirect_url, *, data_directory=None):
    directory = (
        Path(data_directory)
        if data_directory is not None
        else Path(__file__).resolve().parent / "data"
    )
    # A different callback requires its own registered OAuth client.
    suffix = hashlib.sha256(redirect_url.encode()).hexdigest()[:12]
    return directory / ("python-oauth-" + suffix + ".sqlite3")


def create_client(redirect_url, *, data_directory=None):
    return JutsuPythonClient(
        auth_database_path(redirect_url, data_directory=data_directory), redirect_url
    )
