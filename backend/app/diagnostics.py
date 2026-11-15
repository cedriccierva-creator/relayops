"""Host-local diagnostics. No database initialization, payload reads or writes."""
import fcntl
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url


def database_path(settings):
    url = make_url(settings.DATABASE_URL)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:" or url.query:
        raise ValueError("Diagnostics require a plain file-backed SQLite URL")
    return Path(url.database).resolve()


def worker_health(path, max_age, now=None):
    report = {"status": "missing", "age_seconds": None, "max_age_seconds": max_age}
    try:
        with Path(str(path) + ".worker.lock").open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                modified = os.fstat(lock.fileno()).st_mtime
                age = (time.time() if now is None else now) - modified
                report["age_seconds"] = round(age, 3) if modified else None
                report["status"] = ("starting" if not modified else "clock_skew" if age < 0
                                    else "healthy" if age <= max_age else "stale")
            else:
                report["status"] = "stopped"
    except FileNotFoundError:
        pass
    except OSError:
        report["status"] = "unavailable"
    return report


def delivery_diagnostics(path, now=None):
    now = now or datetime.now(timezone.utc)
    timestamp = now.astimezone(timezone.utc).replace(tzinfo=None).isoformat(" ", timespec="microseconds")
    # mode=ro refuses missing databases; query_only also prevents accidental writes.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("""
            SELECT COUNT(*),
                COUNT(CASE WHEN status = 'queued' THEN 1 END),
                COUNT(CASE WHEN status = 'running' THEN 1 END),
                COUNT(CASE WHEN status = 'delivered' THEN 1 END),
                COUNT(CASE WHEN status = 'failed' THEN 1 END),
                COUNT(CASE WHEN status = 'queued' AND next_attempt_at <= ? THEN 1 END),
                COUNT(CASE WHEN status = 'queued' AND next_attempt_at > ? THEN 1 END),
                MIN(CASE WHEN status IN ('queued', 'running') THEN created_at END)
            FROM relay_jobs
        """, (timestamp, timestamp)).fetchone()
    finally:
        connection.close()
    total, queued, running, delivered, failed, due, scheduled, oldest = row
    age = None if oldest is None else max(0, (now - datetime.fromisoformat(oldest).replace(tzinfo=timezone.utc)).total_seconds())
    return {"total_jobs": total, "queued": queued, "running": running,
            "delivered": delivered, "failed": failed,
            "unknown_status": total - queued - running - delivered - failed,
            "queued_due": due, "queued_scheduled": scheduled,
            "outstanding": queued + running, "oldest_outstanding_age_seconds": age}
