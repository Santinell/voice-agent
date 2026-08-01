"""Tests for ``SecretStore`` — the generic stored_secrets table.

Each test migrates a fresh DB and opens a connection; the store injects that
connection. No real network, no shared state between tests.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from db import connect, migrate
from tools.secrets import SecretStore


@pytest.fixture()
def store(tmp_path: Path) -> SecretStore:
    """A SecretStore backed by a freshly migrated agent.db."""
    db_path = tmp_path / "agent.db"
    migrate(db_path)
    conn = connect(db_path)
    return SecretStore(conn)


def test_get_missing_returns_none(store: SecretStore) -> None:
    assert store.get("nope") is None


def test_put_then_get_roundtrips(store: SecretStore) -> None:
    store.put("jina", "jina_key_123")
    got = store.get("jina")
    assert got is not None
    assert got.name == "jina"
    assert got.value == "jina_key_123"
    assert got.expires_at is None


def test_put_overwrites_existing(store: SecretStore) -> None:
    store.put("jina", "old_key")
    store.put("jina", "new_key")
    got = store.get("jina")
    assert got is not None
    assert got.value == "new_key"


def test_put_with_expiry_roundtrips(store: SecretStore) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    store.put("jina", "trial", expires_at=future)
    got = store.get("jina")
    assert got is not None
    assert got.expires_at is not None
    # ISO round-trip preserves the timestamp to the second.
    assert got.expires_at.replace(microsecond=0) == future.replace(microsecond=0)


def test_get_expired_secret_returns_none(store: SecretStore) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    store.put("jina", "stale", expires_at=past)
    assert store.get("jina") is None


def test_delete_removes_secret(store: SecretStore) -> None:
    store.put("jina", "key")
    assert store.delete("jina") is True
    assert store.get("jina") is None


def test_delete_missing_returns_false(store: SecretStore) -> None:
    assert store.delete("never_existed") is False


def test_put_overwrites_expires_at(store: SecretStore) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    store.put("jina", "k", expires_at=future)
    store.put("jina", "k")  # clear expiry
    got = store.get("jina")
    assert got is not None
    assert got.expires_at is None


def test_separate_names_coexist(store: SecretStore) -> None:
    store.put("jina", "jk")
    store.put("exa", "ek")
    assert store.get("jina") is not None and store.get("jina").value == "jk"
    assert store.get("exa") is not None and store.get("exa").value == "ek"


def test_underlying_table_rows(store: SecretStore, tmp_path: Path) -> None:
    # Verify the row is really in stored_secrets with the expected columns.
    store.put("jina", "v")
    conn = sqlite3.connect(str(tmp_path / "agent.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM stored_secrets WHERE name='jina'").fetchone()
    assert row is not None
    assert row["value"] == "v"
    assert row["expires_at"] is None
    assert row["created_at"]
