"""Database bootstrap: open the SQLite connection and run migrations.

The whole app keeps one long-lived ``sqlite3.Connection`` (opened here), shared
by the scheduler store and the secrets store. The schema is owned exclusively
by yoyo migrations under ``migrations/`` — no module creates tables on its own.

Two entry points:

* :func:`migrate` — apply any pending migrations to ``db_path`` (called once at
  startup, before anything opens the connection).
* :func:`connect` — open the connection with the pragmas the stores rely on
  (WAL, ``synchronous=NORMAL``, ``row_factory=Row``, autocommit).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import yoyo  # type: ignore[import-untyped]  # no stubs shipped

# Migrations live next to ``src/`` (one level above this module).
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def db_uri(db_path: Path) -> str:
    """SQLAlchemy-style URI that yoyo accepts: ``sqlite:///<abs path>``."""
    return "sqlite:///" + str(db_path.resolve())


def migrate(db_path: Path) -> None:
    """Apply every pending migration in :data:`_MIGRATIONS_DIR`.

    Idempotent: ``to_apply`` selects only migrations not yet recorded. Safe to
    call on every startup.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # yoyo is untyped; coerce to Any so the call sites stay strict-clean.
    backend: Any = yoyo.get_backend(db_uri(db_path))  # type: ignore[no-any-return]
    try:
        migrations: Any = yoyo.read_migrations(str(_MIGRATIONS_DIR))  # type: ignore[no-any-return]
        pending = backend.to_apply(migrations)
        backend.apply_migrations(pending)
    finally:
        backend.connection.close()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the app's single long-lived connection.

    ``check_same_thread=False`` lets the scheduler's daemon thread share it;
    every store guards access with its own lock. ``isolation_level=None`` puts
    the connection in autocommit — matching how the scheduler store was already
    written.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


__all__ = ["db_uri", "migrate", "connect"]
