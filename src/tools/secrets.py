"""``stored_secrets`` — generic key/value store for API keys.

Holds secrets the app obtains at runtime (a Jina trial key today, possibly
more later). One row per ``name`` with an optional ``expires_at`` so a stale
key can be rotated lazily. The table is created by migration
``0001.create_core_tables``; this module only reads and writes it.

Thread-safe: every method acquires the store's lock, matching the scheduler
store. The connection itself is the app's single shared connection
(``check_same_thread=False``).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StoredSecret:
    """A stored secret row: its value and when it expires (``None`` = never)."""

    name: str
    value: str
    expires_at: datetime | None


class SecretStore:
    """Thread-safe read/write access to the ``stored_secrets`` table.

    The connection is injected (the app owns one for the whole process) and is
    expected to point at a DB whose schema is already migrated.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def get(self, name: str) -> StoredSecret | None:
        """Return the secret, or ``None`` if absent or past ``expires_at``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT name, value, expires_at FROM stored_secrets WHERE name=?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        expires_raw = row["expires_at"]
        expires_at = datetime.fromisoformat(expires_raw) if expires_raw else None
        if expires_at is not None and datetime.now(expires_at.tzinfo) > expires_at:
            return None
        return StoredSecret(name=row["name"], value=row["value"], expires_at=expires_at)

    def put(
        self, name: str, value: str, *, expires_at: datetime | None = None
    ) -> None:
        """Upsert a secret, replacing any previous row for ``name``."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO stored_secrets (name, value, expires_at, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, "
                "expires_at=excluded.expires_at",
                (
                    name,
                    value,
                    expires_at.isoformat() if expires_at else None,
                    datetime.now().isoformat(),
                ),
            )

    def delete(self, name: str) -> bool:
        """Remove a secret; return whether a row was actually deleted."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM stored_secrets WHERE name=?", (name,))
            return cur.rowcount > 0


__all__ = ["StoredSecret", "SecretStore"]
