from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import json
import sqlite3

from .metrics import as_time, iso


class Store:
    """SQLite is the local durable snapshot cache, shared by worker and MCP reader."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "snapshots.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    device_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                    source_at TEXT NOT NULL, eligible INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(device_id, observed_at)
                );
                CREATE INDEX IF NOT EXISTS snapshots_observed ON snapshots(observed_at);
                CREATE TABLE IF NOT EXISTS attempts (
                    run_id TEXT NOT NULL, device_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                    error_code TEXT, PRIMARY KEY(run_id, device_id)
                );
                CREATE INDEX IF NOT EXISTS attempts_device ON attempts(device_id, observed_at DESC);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                    finished_at TEXT, body TEXT
                );
            """)
        self.path.chmod(0o600)

    def connect(self):
        # Always close connections explicitly; sqlite's own context manager only commits.
        return closing(sqlite3.connect(self.path, timeout=30))

    def start_run(self, run_id: str, now: datetime):
        with self.connect() as db, db:
            db.execute("INSERT INTO runs VALUES (?, ?, NULL, NULL)", (run_id, iso(now)))

    def finish_run(self, run_id: str, now: datetime, body: dict):
        with self.connect() as db, db:
            db.execute("UPDATE runs SET finished_at=?, body=? WHERE run_id=?",
                       (iso(now), json.dumps(body), run_id))

    def record(self, run_id: str, serial: str, observed: datetime,
               snapshot: dict | None = None, error: str | None = None):
        with self.connect() as db, db:
            if snapshot is not None:
                eligible = not {"source_clock_in_future", "source_timestamp_reused_with_changed_metrics"}.intersection(snapshot["data_quality"])
                db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                           (serial, snapshot["observed_at"], snapshot["source_timestamp"],
                            int(eligible), json.dumps(snapshot)))
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?)", (run_id, serial, iso(observed), error))

    def latest(self, serial: str, before: str | None = None, baseline=False) -> dict | None:
        query = "SELECT body FROM snapshots WHERE device_id=?"
        args = [serial]
        if before:
            query += " AND observed_at < ?"
            args.append(before)
        if baseline:
            query += " AND eligible=1 ORDER BY source_at DESC, observed_at DESC LIMIT 1"
        else:
            query += " ORDER BY observed_at DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, args).fetchone()
        return json.loads(row[0]) if row else None

    def history(self, serial: str, start: datetime, end: datetime, limit: int) -> dict:
        if start >= end:
            raise ValueError("start must be earlier than end; end is exclusive")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self.connect() as db:
            rows = db.execute("""SELECT body FROM snapshots WHERE device_id=?
                AND observed_at>=? AND observed_at<? ORDER BY observed_at DESC LIMIT ?""",
                              (serial, iso(start), iso(end), limit + 1)).fetchall()
        snapshots = [json.loads(r[0]) for r in rows[:limit]]
        return {"device_id": serial, "start_inclusive": iso(start), "end_exclusive": iso(end),
                "snapshots": snapshots, "has_more": len(rows) > limit,
                "next_end_exclusive": snapshots[-1]["observed_at"] if len(rows) > limit else None,
                "coverage": "Retained polling observations only; no historical backfill."}

    def latest_attempt(self, serial: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("""SELECT run_id, observed_at, error_code FROM attempts
                WHERE device_id=? ORDER BY observed_at DESC LIMIT 1""", (serial,)).fetchone()
        return dict(zip(("run_id", "observed_at", "error_code"), row)) if row else None

    def exact(self, serial: str, observed_at: datetime) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT body FROM snapshots WHERE device_id=? AND observed_at=?",
                             (serial, iso(observed_at))).fetchone()
        return json.loads(row[0]) if row else None

    def device_ids(self) -> tuple[str, ...]:
        with self.connect() as db:
            rows = db.execute("SELECT device_id FROM snapshots UNION SELECT device_id FROM attempts ORDER BY device_id").fetchall()
        return tuple(row[0] for row in rows)

    def health(self, now: datetime, stale_seconds: int, devices: tuple[str, ...]) -> dict:
        with self.connect() as db:
            row = db.execute("""SELECT run_id, started_at, finished_at, body FROM runs
                ORDER BY started_at DESC LIMIT 1""").fetchone()
            coverage = db.execute("SELECT MIN(observed_at), MAX(observed_at), COUNT(*) FROM snapshots").fetchone()
        result = {"known_devices": len(devices), "cached_observations": coverage[2],
                  "history_start": coverage[0], "history_end": coverage[1], "latest_run": None}
        if row:
            age = (now - as_time(row[2] or row[1])).total_seconds()
            result["latest_run"] = {"run_id": row[0], "started_at": row[1], "finished_at": row[2],
                                    "summary": json.loads(row[3]) if row[3] else None}
            result["status"] = "worker_stale" if age > stale_seconds else "running" if not row[2] else "ready"
            if result["status"] == "ready" and result["latest_run"]["summary"].get("failed", 0):
                result["status"] = "degraded"
        else:
            result["status"] = "awaiting_device_request" if not devices else "awaiting_first_poll"
        if not devices:
            result["status"] = "awaiting_device_request"
        return result

    def prune(self, now: datetime, retention_days: int):
        cutoff = iso(now - timedelta(days=retention_days))
        with self.connect() as db, db:
            for table, column in (("snapshots", "observed_at"), ("attempts", "observed_at"), ("runs", "started_at")):
                db.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))

