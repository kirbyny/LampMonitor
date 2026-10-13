"""SQLite persistence for settings, cameras, lamps and the event log."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import Camera, CameraIn, Lamp, LampIn, Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    protect_camera_id TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    poll_interval REAL NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lamps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    metric TEXT NOT NULL DEFAULT 'peak',
    threshold REAL NOT NULL DEFAULT 128,
    hysteresis REAL NOT NULL DEFAULT 10,
    confirm_seconds REAL NOT NULL DEFAULT 15,
    email_to TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    notify_on_restore INTEGER NOT NULL DEFAULT 1,
    repeat_minutes REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'unknown',
    state_since TEXT
);

CREATE INDEX IF NOT EXISTS lamps_camera ON lamps(camera_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    camera_id INTEGER,
    lamp_id INTEGER,
    message TEXT NOT NULL,
    value REAL
);
"""

MAX_EVENTS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin synchronous wrapper around one SQLite connection, safe to share between threads."""

    def __init__(self, path: str | Path) -> None:
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _insert(self, sql: str, params: tuple) -> int:
        with self._lock:
            return self._conn.execute(sql, params).lastrowid

    def _change(self, sql: str, params: tuple) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    # Settings

    def get_settings(self) -> Settings:
        stored = {row["key"]: json.loads(row["value"]) for row in self._rows("SELECT key, value FROM settings")}
        return Settings.model_validate({k: v for k, v in stored.items() if k in Settings.model_fields})

    def save_settings(self, settings: Settings) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for key, value in settings.model_dump().items():
                    self._conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value)),
                    )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # Cameras

    def list_cameras(self) -> list[Camera]:
        return [Camera(**row) for row in self._rows("SELECT * FROM cameras ORDER BY name COLLATE NOCASE, id")]

    def get_camera(self, camera_id: int) -> Camera | None:
        rows = self._rows("SELECT * FROM cameras WHERE id = ?", (camera_id,))
        return Camera(**rows[0]) if rows else None

    def create_camera(self, data: CameraIn) -> Camera:
        values = data.model_dump()
        camera_id = self._insert(
            f"INSERT INTO cameras ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            tuple(values.values()),
        )
        return self.get_camera(camera_id)

    def update_camera(self, camera_id: int, data: CameraIn) -> Camera | None:
        values = data.model_dump()
        assignments = ", ".join(f"{column} = ?" for column in values)
        if not self._change(f"UPDATE cameras SET {assignments} WHERE id = ?", (*values.values(), camera_id)):
            return None
        return self.get_camera(camera_id)

    def delete_camera(self, camera_id: int) -> bool:
        return bool(self._change("DELETE FROM cameras WHERE id = ?", (camera_id,)))

    # Lamps

    def list_lamps(self, camera_id: int | None = None) -> list[Lamp]:
        if camera_id is None:
            rows = self._rows("SELECT * FROM lamps ORDER BY name COLLATE NOCASE, id")
        else:
            rows = self._rows("SELECT * FROM lamps WHERE camera_id = ? ORDER BY name COLLATE NOCASE, id", (camera_id,))
        return [Lamp(**row) for row in rows]

    def get_lamp(self, lamp_id: int) -> Lamp | None:
        rows = self._rows("SELECT * FROM lamps WHERE id = ?", (lamp_id,))
        return Lamp(**rows[0]) if rows else None

    def create_lamp(self, camera_id: int, data: LampIn) -> Lamp:
        values = {"camera_id": camera_id, **data.model_dump()}
        lamp_id = self._insert(
            f"INSERT INTO lamps ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            tuple(values.values()),
        )
        return self.get_lamp(lamp_id)

    def update_lamp(self, lamp_id: int, data: LampIn) -> Lamp | None:
        values = data.model_dump()
        assignments = ", ".join(f"{column} = ?" for column in values)
        if not self._change(f"UPDATE lamps SET {assignments} WHERE id = ?", (*values.values(), lamp_id)):
            return None
        return self.get_lamp(lamp_id)

    def delete_lamp(self, lamp_id: int) -> bool:
        return bool(self._change("DELETE FROM lamps WHERE id = ?", (lamp_id,)))

    def set_lamp_state(self, lamp_id: int, state: str, since: datetime | None) -> None:
        self._change(
            "UPDATE lamps SET state = ?, state_since = ? WHERE id = ?",
            (state, since.isoformat(timespec="seconds") if since else None, lamp_id),
        )

    # Events

    def add_event(
        self,
        kind: str,
        message: str,
        *,
        camera_id: int | None = None,
        lamp_id: int | None = None,
        value: float | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, kind, camera_id, lamp_id, message, value) VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), kind, camera_id, lamp_id, message, value),
            )
            self._conn.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (MAX_EVENTS,))

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
