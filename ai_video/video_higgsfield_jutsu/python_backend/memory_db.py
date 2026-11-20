"""Process-local SQLite state without database or temporary files."""

from contextlib import contextmanager
import sqlite3
from threading import RLock


class MemoryDatabase:
    def __init__(self, *, row_factory=None):
        self._lock = RLock()
        self._connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._connection.row_factory = row_factory
        self._connection.execute("PRAGMA temp_store=MEMORY")

    @contextmanager
    def db(self):
        # Keep the connection alive between calls; serialize complete transactions
        # so background threads cannot commit or roll back each other's writes.
        with self._lock:
            with self._connection:
                yield self._connection

    def close(self):
        with self._lock:
            self._connection.close()
