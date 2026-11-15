import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select

from app.config import get_settings
from app.ingestion_limits import SourceRateLimiter
from app.models import ReceivedEvent
from app.routers.hooks import ingest as ingest_route
from tests.test_relay import SECRET, ingest, public_dns, setup  # Reuse isolated fixtures.


@pytest.mark.parametrize("size,status", [(1023, 202), (1024, 202), (1025, 413)])
@pytest.mark.parametrize("length", ["actual", None, "1", "9999", "invalid", "-1"])
def test_body_limits(client, setup, monkeypatch, size, status, length):
    source, _, factory = setup
    monkeypatch.setattr(get_settings(), "WEBHOOK_MAX_BYTES", 1024)
    body = b'"' + b'x' * (size - 2) + b'"'
    headers = [(b'content-type', b'application/json'), (b'x-webhook-secret', SECRET.encode())]
    if length is not None:
        headers.append((b'content-length', str(size).encode() if length == "actual" else length.encode()))
    if length == "9999":
        status = 413
    elif length in ("invalid", "-1"):
        status = 400
    # Real ASGI chunks: TestClient can combine generator chunks before delivery.
    chunks = iter([body[:512], body[512:], b''])
    reads = []

    async def receive():
        chunk = next(chunks)
        reads.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

    async def run():
        request = Request({"type": "http", "headers": headers}, receive)
        with factory() as db:
            try:
                await ingest_route(source["id"], request, Response(), db)
                return 202
            except HTTPException as exc:
                return exc.status_code

    assert asyncio.run(run()) == status
    if status == 413:
        assert len(reads) <= 2  # Rejection never drains the rest of the stream.
    with factory() as db:
        assert db.scalar(select(func.count(ReceivedEvent.id))) == (1 if status == 202 else 0)


def test_rate_limit_auth_deduplication_and_source_isolation(client, setup, monkeypatch):
    source, _, factory = setup
    monkeypatch.setattr(get_settings(), "WEBHOOK_RATE_LIMIT", 2)
    path = f'/api/hooks/{source["id"]}'
    for target in (path, "/api/hooks/unknown"):
        response = client.post(target, json={}, headers={"X-Webhook-Secret": "wrong"})
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid webhook credentials"}
    first = ingest(client, source, **{"Idempotency-Key": "same"})
    duplicate = ingest(client, source, **{"Idempotency-Key": "same"})
    assert first.status_code == 202 and duplicate.status_code == 200
    assert first.json()["event_id"] == duplicate.json()["event_id"]
    limited = ingest(client, source)
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert client.post(path, json={}, headers={"X-Webhook-Secret": "wrong"}).status_code == 401
    other = client.post("/api/sources", json={"name": "Other", "secret": SECRET}).json()
    assert ingest(client, other).status_code == 202
    assert client.get("/api/auth/me").status_code == 200
    with factory() as db:
        assert db.scalar(select(func.count(ReceivedEvent.id))) == 2


def test_window_expiration_and_bounded_memory():
    now = [0.0]
    limiter = SourceRateLimiter(2, 60, 2, clock=lambda: now[0])
    assert limiter.retry_after("a") == limiter.retry_after("a") == 0
    assert limiter.retry_after("a") == 60
    now[0] = 10
    assert limiter.retry_after("b") == 0
    for i in range(1000):
        assert limiter.retry_after(str(i)) == 50
    assert len(limiter.windows) == 2
    assert limiter.retry_after("a") == 50  # Saturation never evicts an active quota.
    now[0] = 60
    assert limiter.retry_after("a") == 0
    assert limiter.retry_after("b") == 0
    assert limiter.retry_after("b") == 10
    now[0] = 120
    assert limiter.retry_after("new") == 0
    assert len(limiter.windows) == 1


def test_concurrent_quota_is_atomic():
    limiter = SourceRateLimiter(5, 60, 10)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.retry_after("source"), range(50)))
    assert results.count(0) == 5
