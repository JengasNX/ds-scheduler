"""Event-driven task rules: run registered actions on Discord events.

Rules duplicate the /schedule add shape (task_id + action + params) but fire on
guild events instead of timers, with the event context available to variable
placeholders ({user.*}, {channel.*}, {server.*}).
"""
from __future__ import annotations

import logging
import sqlite3
import threading

log = logging.getLogger(__name__)

EVENT_TYPES = (
    "member_join",       # участник зашёл на сервер
    "member_remove",     # участник покинул сервер
    "member_ban",        # участник забанен
    "member_unban",      # бан снят
    "member_update",     # изменение ника/ролей/статуса
    "voice_join",        # подключился к голосовому каналу
    "voice_leave",       # вышел из голосового канала
    "voice_switch",      # перешёл в другой голосовой канал
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_rules (
    event TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (event, task_id)
);
CREATE INDEX IF NOT EXISTS idx_event_rules_event ON event_rules (event);
"""


class EventRules:
    """SQLite-backed mapping: Discord event -> action to run."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, event: str, task_id: str, action: str, params: str) -> None:
        """Create or replace a rule (event, task_id) pair."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO event_rules (event, task_id, action, params) VALUES (?,?,?,?)"
                " ON CONFLICT(event, task_id) DO UPDATE SET action=excluded.action, params=excluded.params",
                (event, task_id, action, params),
            )
            self._conn.commit()
        log.info("Added event rule %s/%s -> %s", event, task_id, action)

    def remove(self, task_id: str, event: str | None = None) -> int:
        """Delete rules by task_id, optionally narrowed to one event type."""
        with self._lock:
            if event is not None:
                cursor = self._conn.execute(
                    "DELETE FROM event_rules WHERE task_id = ? AND event = ?", (task_id, event)
                )
            else:
                cursor = self._conn.execute("DELETE FROM event_rules WHERE task_id = ?", (task_id,))
            self._conn.commit()
            deleted = cursor.rowcount
        if deleted:
            log.info("Removed %d event rule(s) for task %r", deleted, task_id)
        return deleted

    def for_event(self, event: str) -> list[tuple[str, str, str]]:
        """Return [(task_id, action, params_json)] rules bound to the event type."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, action, params FROM event_rules WHERE event = ? ORDER BY created_at, task_id",
                (event,),
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def list_all(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event, task_id, action, created_at FROM event_rules ORDER BY event, task_id"
            ).fetchall()
        return [{"event": row[0], "task_id": row[1], "action": row[2], "created_at": row[3]} for row in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            log.exception("Failed to close the event rule store cleanly.")
