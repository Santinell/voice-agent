"""Tests for the DB layer: migrations create both tables and are idempotent."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db import connect, db_uri, migrate


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """A fresh agent.db path (migrations not yet applied)."""
    return tmp_path / "agent.db"


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_migrate_creates_both_tables(db_path: Path) -> None:
    migrate(db_path)
    conn = sqlite3.connect(str(db_path))
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "scheduled_events" in tables
    assert "stored_secrets" in tables


def test_migrate_columns_match_expected_schema(db_path: Path) -> None:
    migrate(db_path)
    conn = sqlite3.connect(str(db_path))
    assert _table_columns(conn, "scheduled_events") == [
        "id",
        "kind",
        "label",
        "fire_at",
        "weekdays",
        "enabled",
        "created_at",
    ]
    assert _table_columns(conn, "stored_secrets") == [
        "name",
        "value",
        "expires_at",
        "created_at",
    ]


def test_migrate_is_idempotent(db_path: Path) -> None:
    migrate(db_path)
    migrate(db_path)  # second run must not raise
    migrate(db_path)
    conn = sqlite3.connect(str(db_path))
    assert _table_columns(conn, "scheduled_events")  # table intact


def test_migrate_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "dir" / "agent.db"
    migrate(nested)
    assert nested.exists()


def test_connect_sets_pragmas_and_row_factory(db_path: Path) -> None:
    migrate(db_path)
    conn = connect(db_path)
    assert conn.row_factory is sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()


def test_db_uri_is_sqlalchemy_style(db_path: Path) -> None:
    assert db_uri(db_path) == "sqlite:///" + str(db_path.resolve())
