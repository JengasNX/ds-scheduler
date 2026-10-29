"""SQLite-backed execution history for scheduled tasks.

Deliberately synchronous: job functions run in APScheduler's thread-pool
executor, so the log must be callable both from worker threads and from the
event loop. All queries are single tiny statements guarded by a lock.
"""
from __future__ import annotations

import logging
import sqlite3
import threading

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'error')),
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_task ON job_runs (task_id, id DESC);
"""


class RunLog:
    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(
        self,
        task_id: str,
        action: str,
        started_at: str,
        finished_at: str,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_runs (task_id, action, started_at, finished_at, status, error) VALUES (?,?,?,?,?,?)",
                (task_id, action, started_at, finished_at, status, error),
            )
            self._conn.commit()

    def recent(self, task_id: str, limit: int = 10) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT started_at, finished_at, status, error FROM job_runs"
                " WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [
            {"started_at": row[0], "finished_at": row[1], "status": row[2], "error": row[3]}
            for row in rows
        ]

    def stats(self, task_id: str) -> tuple[int, int]:
        """Return (successes, failures) for the task."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM job_runs WHERE task_id = ? GROUP BY status",
                (task_id,),
            ).fetchall()
        counts = {status: count for status, count in rows}
        return counts.get("success", 0), counts.get("error", 0)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            log.exception("Failed to close the run log cleanly.")
