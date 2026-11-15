import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import event as sql_event, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import DeliveryAttempt, DeliveryJob, Destination, ReceivedEvent, User, WebhookSource, utcnow
from app.retention import plan_retention


@pytest.fixture()
def retention_data(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "RETENTION_ENABLED", True)
    factory = sessionmaker(bind=engine)
    now = utcnow()
    ids = {}
    with factory.begin() as db:
        user = User(email="retention@example.com", hashed_password="unused")
        db.add(user)
        db.flush()
        source = WebhookSource(owner_id=user.id, name="Test", secret_hash="unused")
        db.add(source)
        db.flush()
        dest = Destination(source_id=source.id, name="Test", url="https://example.com")
        db.add(dest)
        db.flush()
        for label in ("delivered", "failed", "recent", "boundary", "queued", "retry", "running",
                      "unrouted", "unknown", "mixed", "claimed", "unfinished"):
            age = 1 if label == "recent" else 30 if label == "boundary" else 31
            event = ReceivedEvent(source_id=source.id, payload='{"private":"retention-test"}',
                                  payload_hash="digest", size_bytes=28, received_at=now - timedelta(days=age))
            db.add(event)
            db.flush()
            ids[label] = event.id
            if label == "unrouted":
                continue
            state = {"retry": "queued", "recent": "delivered", "boundary": "delivered",
                     "mixed": "failed", "claimed": "failed", "unfinished": "failed"}.get(label, label)
            job = DeliveryJob(event_id=event.id, destination_id=dest.id, status=state, max_attempts=5,
                              next_attempt_at=now + timedelta(days=1),
                              claim_token="active" if label == "claimed" else None)
            db.add(job)
            db.flush()
            db.add(DeliveryAttempt(job_id=job.id, number=1, status_code=500, error="http_error",
                                   finished_at=None if label == "unfinished" else now))
            if label == "mixed":
                other = Destination(source_id=source.id, name="Other", url="https://example.com/other")
                db.add(other)
                db.flush()
                db.add(DeliveryJob(event_id=event.id, destination_id=other.id, status="queued", max_attempts=5))
    return factory, now, ids


def snapshot(factory):
    with factory() as db:
        return [list(db.execute(select(table)).all())
                for table in (ReceivedEvent.__table__, DeliveryJob.__table__, DeliveryAttempt.__table__)]


def test_retention_disabled_does_not_open_database():
    assert get_settings().RETENTION_ENABLED is False
    def forbidden():
        pytest.fail("disabled retention opened database")
    assert plan_retention(forbidden, get_settings())["events"] == []


def test_plan_safety_history_and_repeated_runs(retention_data):
    factory, now, ids = retention_data
    before = snapshot(factory)
    result = plan_retention(factory, get_settings(), now)
    assert {row["event_id"] for row in result["events"]} == {ids[k] for k in ("delivered", "failed", "unrouted")}
    assert result["dry_run"] is True and result["cleanup_supported"] is False
    assert result["candidate_bytes"] == 84 and not result["has_more"]
    assert "retention-test" not in json.dumps(result)
    assert plan_retention(factory, get_settings(), now) == result
    assert snapshot(factory) == before  # Payloads, jobs, and attempt diagnostics unchanged.


def test_plan_bounded(retention_data, monkeypatch):
    factory, now, _ = retention_data
    monkeypatch.setattr(get_settings(), "RETENTION_BATCH_SIZE", 1)
    result = plan_retention(factory, get_settings(), now)
    assert len(result["events"]) == 1 and result["has_more"]


def test_plan_failure_rolls_back_read_transaction(retention_data, engine):
    factory, now, _ = retention_data
    before = snapshot(factory)
    rollbacks = []
    def fail(*args):
        raise RuntimeError("injected query failure")
    def rolled_back(connection):
        rollbacks.append(True)
    sql_event.listen(engine, "before_cursor_execute", fail)
    sql_event.listen(engine, "rollback", rolled_back)
    try:
        with pytest.raises(RuntimeError, match="injected query failure"):
            plan_retention(factory, get_settings(), now)
    finally:
        sql_event.remove(engine, "before_cursor_execute", fail)
        sql_event.remove(engine, "rollback", rolled_back)
    assert rollbacks
    assert snapshot(factory) == before


def test_cli_only_supports_dry_run(retention_data, engine):
    factory, _, _ = retention_data
    before = snapshot(factory)
    env = {**os.environ, "DATABASE_URL": str(engine.url), "RETENTION_ENABLED": "true"}
    result = subprocess.run([sys.executable, "retention.py", "--dry-run"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cleanup_supported"] is False
    rejected = subprocess.run([sys.executable, "retention.py", "--execute"], env=env, capture_output=True)
    assert rejected.returncode == 2
    assert snapshot(factory) == before
