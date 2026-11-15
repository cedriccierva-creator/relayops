import asyncio
import fcntl
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.delivery import claim_job, finish_job
from app.diagnostics import delivery_diagnostics, worker_health
from tests.test_retention import retention_data, snapshot
from worker import cycle_heartbeat


def test_worker_health_and_restart(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    assert worker_health(path, 120)["status"] == "missing"
    assert not list(tmp_path.iterdir())
    with (tmp_path / "jobs.db.worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tick = cycle_heartbeat(lock)
        assert worker_health(path, 120)["status"] == "starting"
        monkeypatch.setattr("worker.time.monotonic", lambda: 100)
        tick()
        stamp = os.fstat(lock.fileno()).st_mtime
        assert worker_health(path, 120, stamp + 120)["status"] == "healthy"
        assert worker_health(path, 15, stamp + 16)["status"] == "stale"
        assert worker_health(path, 120, stamp - 1)["status"] == "clock_skew"
        tick()
        assert os.fstat(lock.fileno()).st_mtime == stamp
        monkeypatch.setattr("worker.time.monotonic", lambda: 110)
        os.utime(lock.fileno(), (1, 1))
        tick()
        assert os.fstat(lock.fileno()).st_mtime > 1
        restarted = cycle_heartbeat(lock)
        assert worker_health(path, 120)["status"] == "starting"
        restarted()
        assert worker_health(path, 120)["status"] == "healthy"
    assert worker_health(path, 120)["status"] == "stopped"


@pytest.mark.parametrize("value", [14, 3601])
def test_threshold_bounds(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, WORKER_HEALTH_MAX_AGE_SECONDS=value)


def test_no_public_diagnostics(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/diagnostics").status_code == 404
    assert client.get("/api/stats").status_code == 401


def test_permission_failure_is_unavailable(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("private-path")
    monkeypatch.setattr("pathlib.Path.open", denied)
    assert worker_health(tmp_path / "jobs.db", 120)["status"] == "unavailable"


def test_empty_and_missing_database(engine, tmp_path):
    result = delivery_diagnostics(tmp_path / "test.db")
    assert result["total_jobs"] == result["outstanding"] == 0
    assert result["oldest_outstanding_age_seconds"] is None
    with pytest.raises(sqlite3.OperationalError):
        delivery_diagnostics(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_mixed_diagnostics_read_only(retention_data, engine, tmp_path):
    factory, now, _ = retention_data
    before = snapshot(factory)
    result = delivery_diagnostics(tmp_path / "test.db", now)
    assert result == {"total_jobs": 12, "queued": 3, "running": 1, "delivered": 3,
                      "failed": 4, "unknown_status": 1, "queued_due": 0, "queued_scheduled": 3,
                      "outstanding": 4, "oldest_outstanding_age_seconds": 0}
    later = delivery_diagnostics(tmp_path / "test.db", now + timedelta(days=2))
    assert later["queued_due"] == 3 and later["queued_scheduled"] == 0
    assert 172790 < later["oldest_outstanding_age_seconds"] <= 172800
    env = {**os.environ, "DATABASE_URL": str(engine.url), "WORKER_HEALTH_MAX_AGE_SECONDS": "45"}
    process = subprocess.run([sys.executable, "diagnostics.py"], env=env, capture_output=True, text=True, timeout=10)
    assert process.returncode == 1
    report = json.loads(process.stdout)
    assert report["worker"]["max_age_seconds"] == 45
    assert "retention-test" not in process.stdout
    assert snapshot(factory) == before


@pytest.mark.parametrize("code,error,classification,result", [
    (200, None, "none", "delivered"), (404, "permanent_http_error", "permanent", "failed"),
    (503, "http_error", "transient", "retry_scheduled"), (None, "timeout", "transient", "retry_scheduled"),
    (None, "private-secret", "permanent", "failed"),
])
def test_delivery_logs(retention_data, caplog, code, error, classification, result):
    factory, _, _ = retention_data
    from app.models import DeliveryJob, utcnow
    from sqlalchemy import update
    with factory.begin() as db:
        db.execute(update(DeliveryJob).where(DeliveryJob.status == "queued").values(next_attempt_at=utcnow(), attempts_total=1))
    claim = claim_job(factory, get_settings())
    claim["url"] = "https://user:private-secret@example.com/?token=private-secret"
    claim["payload"] = "private-secret"
    outcome = {"status_code": code, "error": error, "duration_ms": 1, "response_metadata": {}}
    with caplog.at_level(logging.INFO, logger="app.delivery"):
        finish_job(factory, claim, outcome, get_settings())
        finish_job(factory, claim, outcome, get_settings())  # stale completion emits no success log
    assert len(caplog.records) == 1
    assert claim["event_id"] in caplog.text and claim["id"] in caplog.text
    assert "attempt=2" in caplog.text
    assert f"failure={classification}" in caplog.text and f"result={result}" in caplog.text
    assert "private-secret" not in caplog.text and "https://" not in caplog.text


def test_only_successful_cycles_refresh_heartbeat(monkeypatch):
    import signal
    import worker
    async def exercise():
        handlers, beats = {}, []
        calls = 0
        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda sig, cb: handlers.setdefault(sig, cb))
        async def cycle(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("private")
            handlers[signal.SIGTERM]()
            return True
        monkeypatch.setattr(worker, "run_once", cycle)
        await asyncio.wait_for(worker.serve(object(), lambda: beats.append(calls)), 3)
        assert beats == [2]
    asyncio.run(exercise())
