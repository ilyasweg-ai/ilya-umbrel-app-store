from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


ACTIVE_STATUSES = ("pending", "waiting_file_copy", "probing", "converting")


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL UNIQUE,
                    output_path TEXT,
                    temp_output_path TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    source_codec TEXT,
                    source_width INTEGER,
                    source_height INTEGER,
                    source_duration REAL,
                    source_size_bytes INTEGER,
                    output_size_bytes INTEGER,
                    progress_percent REAL NOT NULL DEFAULT 0,
                    fps REAL,
                    speed TEXT,
                    eta_seconds REAL,
                    started_at TEXT,
                    finished_at TEXT,
                    elapsed_seconds REAL,
                    error_message TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            now = utc_now()
            conn.execute(
                """
                UPDATE jobs
                   SET status = 'pending',
                       progress_percent = 0,
                       updated_at = ?
                 WHERE status IN ('probing', 'converting')
                """,
                (now,),
            )

    def add_or_touch_job(self, source_path: str, source_size: int, status: str) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    source_path, status, source_size_bytes,
                    created_at, updated_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_path, status, source_size, now, now, now),
            )
            inserted = cur.rowcount > 0
            if not inserted:
                conn.execute(
                    """
                    UPDATE jobs
                       SET last_seen_at = ?,
                           source_size_bytes = COALESCE(source_size_bytes, ?)
                     WHERE source_path = ?
                    """,
                    (now, source_size, source_path),
                )
            return inserted

    def promote_waiting(self, source_path: str) -> None:
        self.update_job(source_path=source_path, status="pending", progress_percent=0)

    def update_job(self, job_id: int | None = None, source_path: str | None = None, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        names = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        if job_id is not None:
            where = "id = ?"
            values.append(job_id)
        elif source_path is not None:
            where = "source_path = ?"
            values.append(source_path)
        else:
            raise ValueError("job_id or source_path is required")
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {names} WHERE {where}", values)

    def get_next_job(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM jobs
                 WHERE status = 'pending'
                 ORDER BY created_at ASC
                 LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def waiting_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE status = 'waiting_file_copy'").fetchall()
            return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
            counts = {row["status"]: row["count"] for row in rows}
            sums = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status IN ('success', 'skipped') THEN source_size_bytes ELSE 0 END), 0)
                        AS source_bytes,
                    COALESCE(SUM(CASE WHEN status = 'success' THEN output_size_bytes ELSE 0 END), 0)
                        AS output_bytes
                  FROM jobs
                """
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
        source_bytes = int(sums["source_bytes"] or 0)
        output_bytes = int(sums["output_bytes"] or 0)
        saved_bytes = max(0, source_bytes - output_bytes)
        saved_percent = (saved_bytes / source_bytes * 100) if source_bytes else 0
        processed = sum(counts.get(name, 0) for name in ("success", "skipped", "failed", "moved_to_failed"))
        pending = sum(counts.get(name, 0) for name in ("pending", "waiting_file_copy", "probing", "converting"))
        return {
            "total_jobs": total,
            "processed_jobs": processed,
            "pending_jobs": pending,
            "success_jobs": counts.get("success", 0),
            "failed_jobs": counts.get("failed", 0) + counts.get("moved_to_failed", 0),
            "skipped_jobs": counts.get("skipped", 0),
            "source_bytes": source_bytes,
            "output_bytes": output_bytes,
            "saved_bytes": saved_bytes,
            "saved_percent": round(saved_percent, 2),
            "by_status": counts,
        }

