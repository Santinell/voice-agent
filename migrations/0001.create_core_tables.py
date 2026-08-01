"""Create the core tables: scheduled_events and stored_secrets.

``scheduled_events`` carries the timer/alarm/reminder queue (the same shape the
old ad-hoc schema used). ``stored_secrets`` is a small generic key/value table
for API keys that the app obtains at runtime (e.g. a Jina trial key), with an
optional expiry so they can be rotated.
"""

from yoyo import step

__depends__ = {}

steps = [
    step(
        """
        CREATE TABLE scheduled_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT    NOT NULL,
            label      TEXT,
            fire_at    TEXT    NOT NULL,
            weekdays   TEXT,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL
        )
        """
    ),
    step(
        """
        CREATE TABLE stored_secrets (
            name       TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            expires_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    ),
]
