"""
session_store.py
─────────────────
Persistent SQLite store for two purposes:

1. Vehicle session history — used by the ValidatorAgent for Z-score anomaly detection.
   Table: sessions(vehicle_id, co2_mg, timestamp, validated, record_id)

2. API job store — replaces the in-memory dict, survives server restarts.
   Table: jobs(job_id, status, vehicle_id, recipient, created_at, started_at,
               ended_at, result_json, error)

All writes are synchronous SQLite (thread-safe via check_same_thread=False + mutex).
For multi-process deployments, switch to PostgreSQL or Redis.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

import config

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    """Create tables if they don't exist. Call once at startup."""
    with _lock, _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id TEXT    NOT NULL,
                co2_mg     REAL    NOT NULL,
                timestamp  TEXT    NOT NULL,
                validated  INTEGER NOT NULL DEFAULT 0,
                record_id  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_vehicle ON sessions(vehicle_id);

            CREATE TABLE IF NOT EXISTS jobs (
                job_id      TEXT PRIMARY KEY,
                status      TEXT NOT NULL,
                vehicle_id  TEXT NOT NULL,
                recipient   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                started_at  TEXT,
                ended_at    TEXT,
                result_json TEXT,
                error       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_vehicle ON jobs(vehicle_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
        """)


# ── Session history ───────────────────────────────────────────────────────────

def save_session(
    vehicle_id: str,
    co2_mg: float,
    validated: bool,
    record_id: Optional[int] = None,
) -> None:
    """Persist a completed pipeline session for future anomaly detection."""
    ts = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO sessions(vehicle_id, co2_mg, timestamp, validated, record_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (vehicle_id, co2_mg, ts, int(validated), record_id),
        )


def get_vehicle_history(vehicle_id: str, n: int = 20) -> List[float]:
    """
    Return the last N validated CO2 readings (mg) for a vehicle.
    Only validated sessions are used for the Z-score baseline.
    """
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT co2_mg FROM sessions "
            "WHERE vehicle_id=? AND validated=1 "
            "ORDER BY timestamp DESC LIMIT ?",
            (vehicle_id, n),
        ).fetchall()
    return [row["co2_mg"] for row in rows]


def get_last_session_time(vehicle_id: str) -> Optional[str]:
    """Return ISO timestamp of the most recent session for rate-gap enforcement."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT timestamp FROM sessions WHERE vehicle_id=? ORDER BY timestamp DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()
    return row["timestamp"] if row else None


# ── Job store ─────────────────────────────────────────────────────────────────

def create_job(job_id: str, vehicle_id: str, recipient: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO jobs(job_id, status, vehicle_id, recipient, created_at) "
            "VALUES (?, 'pending', ?, ?, ?)",
            (job_id, vehicle_id, recipient, ts),
        )


def update_job_status(
    job_id: str,
    status: str,
    result: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        if status == "running":
            c.execute(
                "UPDATE jobs SET status=?, started_at=? WHERE job_id=?",
                (status, ts, job_id),
            )
        else:
            c.execute(
                "UPDATE jobs SET status=?, ended_at=?, result_json=?, error=? WHERE job_id=?",
                (status, ts, json.dumps(result, default=str) if result else None, error, job_id),
            )


def get_job(job_id: str) -> Optional[dict]:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("result_json"):
        d["result"] = json.loads(d["result_json"])
    del d["result_json"]
    return d


def list_jobs(limit: int = 20) -> List[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT job_id, status, vehicle_id, recipient, created_at, ended_at "
            "FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_vehicle_jobs_last_hour(vehicle_id: str) -> int:
    """Used for per-vehicle rate limiting."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE vehicle_id=? AND created_at > ?",
            (vehicle_id, cutoff),
        ).fetchone()
    return row["cnt"] if row else 0
