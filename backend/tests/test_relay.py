import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event as sql_event, func, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.delivery import claim_job, finish_job, recover_expired, run_once, send_event
from app.models import DeliveryAttempt, DeliveryJob, ReceivedEvent, utcnow
from app.url_security import UnsafeDestination, parse_destination, resolve_destination
from tests.conftest import sign_up_and_log_in

SECRET = "test-webhook-secret-with-at-least-32-characters"
PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    resolver = AsyncMock(return_value=[(2, 1, 6, "", (PUBLIC, 443))])
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)
    return resolver


@pytest.fixture()
def setup(client, engine):
    sign_up_and_log_in(client)
    source = client.post("/api/sources", json={"name": "Orders", "secret": SECRET}).json()
    res = client.post("/api/destinations", json={"source_id": source["id"], "name": "n8n", "url": "https://hooks.example.com/orders"})
    assert res.status_code == 201, res.text
    return source, res.json(), sessionmaker(bind=engine, expire_on_commit=False)


def ingest(client, source, payload=None, **headers):
    return client.post(f'/api/hooks/{source["id"]}', json=payload if payload is not None else {"order": 42},
                       headers={"X-Webhook-Secret": SECRET, **headers})


def job_for(client, event_id):
    return client.get(f"/api/events/{event_id}").json()["jobs"][0]


def test_ingestion_atomic_fanout_metadata_and_secrets(client, setup):
    source, destination, factory = setup
    res = ingest(client, source, {"order": 42, "api_key": "hidden", "nested": {"password": "hidden"}}, Authorization="Bearer private")
    assert res.status_code == 202
    detail = client.get(f'/api/events/{res.json()["event_id"]}').json()
    assert detail["status"] == "queued"
    assert detail["payload"] == {"order": 42, "api_key": "[REDACTED]", "nested": {"password": "[REDACTED]"}}
    assert detail["request_metadata"] == {"content_type": "application/json", "has_idempotency_key": False}
    assert detail["received_at"].endswith(("Z", "+00:00"))
    assert len(detail["jobs"]) == 1 and detail["jobs"][0]["destination_id"] == destination["id"]
    assert "secret" not in json.dumps(client.get("/api/sources").json())
    with factory() as db:
        assert db.scalar(select(func.count(ReceivedEvent.id))) == 1
        assert db.scalar(select(func.count(DeliveryJob.id))) == 1
        assert "hidden" in db.scalar(select(ReceivedEvent.payload))


@pytest.mark.parametrize("body", [b'{"broken":', b'not-json', b'{"x":NaN}', b'{"x":Infinity}', b'\xff', b''])
def test_invalid_json(client, setup, body):
    source, _, _ = setup
    res = client.post(f'/api/hooks/{source["id"]}', content=body, headers={"Content-Type": "application/json", "X-Webhook-Secret": SECRET})
    assert res.status_code == 400


def test_oversized_body_declared_and_streamed(client, setup, monkeypatch):
    source, _, _ = setup
    monkeypatch.setattr(get_settings(), "WEBHOOK_MAX_BYTES", 1024)
    headers = {"Content-Type": "application/json", "X-Webhook-Secret": SECRET}
    assert client.post(f'/api/hooks/{source["id"]}', content=b'"' + b'x' * 1100 + b'"', headers=headers).status_code == 413
    def chunks():
        yield b'"' + b'x' * 600
        yield b'x' * 600 + b'"'
    assert client.post(f'/api/hooks/{source["id"]}', content=chunks(), headers=headers).status_code == 413
    assert client.get("/api/stats").json()["events"] == 0


def test_unauthorized_disabled_unknown_and_media_types(client, setup):
    source, _, _ = setup
    path = f'/api/hooks/{source["id"]}'
    assert client.post(path, json={}).status_code == 401
    assert client.post(path, json={}, headers={"X-Webhook-Secret": "wrong"}).status_code == 401
    assert client.post("/api/hooks/unknown", json={}, headers={"X-Webhook-Secret": SECRET}).status_code == 401
    assert client.post(path, content='{}', headers={"X-Webhook-Secret": SECRET}).status_code == 415
    assert ingest(client, source, **{"Content-Encoding": "gzip"}).status_code == 415
    assert client.patch(f'/api/sources/{source["id"]}', json={"enabled": False}).status_code == 200
    assert ingest(client, source).status_code == 401


def test_source_scoped_duplicates_and_conflict(client, setup):
    source, _, factory = setup
    first = ingest(client, source, **{"Idempotency-Key": "same"})
    second = ingest(client, source, **{"Idempotency-Key": "same"})
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["event_id"] == second.json()["event_id"] and second.json()["duplicate"] is True
    assert ingest(client, source, {"order": 43}, **{"Idempotency-Key": "same"}).status_code == 409
    other = client.post("/api/sources", json={"name": "Other", "secret": SECRET}).json()
    assert ingest(client, other, **{"Idempotency-Key": "same"}).status_code == 202
    with factory() as db:
        assert db.scalar(select(func.count(ReceivedEvent.id))) == 2
        assert db.scalar(select(func.count(DeliveryJob.id))) == 1


def test_missing_key_x_event_id_and_unrouted(client, setup):
    source, destination, _ = setup
    assert ingest(client, source, **{"Idempotency-Key": ""}).status_code == 400
    assert ingest(client, source, **{"Idempotency-Key": " "}).status_code == 400
    assert ingest(client, source).json()["event_id"] != ingest(client, source).json()["event_id"]
    ingest(client, source, **{"X-Event-ID": "vendor-123"})
    assert ingest(client, source, **{"X-Event-ID": "vendor-123"}).json()["duplicate"] is True
    client.patch(f'/api/destinations/{destination["id"]}', json={"enabled": False})
    event_id = ingest(client, source).json()["event_id"]
    detail = client.get(f"/api/events/{event_id}").json()
    assert detail["status"] == "unrouted" and detail["jobs"] == []


def test_successful_delivery_uses_pinned_ip_and_no_credentials(client, setup):
    source, _, factory = setup
    event_id = ingest(client, source).json()["event_id"]
    job = job_for(client, event_id)
    def destination(request):
        assert request.url.host == PUBLIC
        assert request.headers["host"] == "hooks.example.com"
        assert request.extensions["sni_hostname"] == "hooks.example.com"
        assert request.headers["idempotency-key"] == job["id"]
        assert request.headers["x-relayops-event-id"] == event_id
        assert "x-webhook-secret" not in request.headers and "cookie" not in request.headers
        assert json.loads(request.content) == {"order": 42}
        return httpx.Response(204, headers={"Content-Type": "application/json", "Set-Cookie": "private=secret"})
    assert asyncio.run(run_once(factory, get_settings(), httpx.MockTransport(destination))) is True
    assert job_for(client, event_id)["status"] == "delivered"
    attempts = client.get(f'/api/deliveries/{job["id"]}/attempts').json()
    assert attempts[0]["status_code"] == 204 and attempts[0]["error"] is None
    assert attempts[0]["duration_ms"] >= 0
    assert attempts[0]["response_metadata"] == {"content_type": "application/json"}
    assert client.get("/api/stats").json()["success_rate"] == 100
    assert asyncio.run(run_once(factory, get_settings(), httpx.MockTransport(destination))) is False


@pytest.mark.parametrize("kind,expected", [("http", "http_error"), ("timeout", "timeout"), ("connect", "connection_error"), ("redirect", "redirect_blocked")])
def test_delivery_failures(client, setup, kind, expected):
    source, _, factory = setup
    event_id = ingest(client, source).json()["event_id"]
    def destination(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("secret must not appear")
        if kind == "connect":
            raise httpx.ConnectError("secret must not appear")
        return httpx.Response(302 if kind == "redirect" else 503, headers={"Location": "http://169.254.169.254/"}, text="private response")
    transport = httpx.MockTransport(destination)
    asyncio.run(run_once(factory, get_settings(), transport))
    job = job_for(client, event_id)
    assert job["status"] == ("failed" if kind == "redirect" else "queued") and job["cycle_attempts"] == 1
    attempts = client.get(f'/api/deliveries/{job["id"]}/attempts').json()
    assert attempts[0]["error"] == expected
    assert "private response" not in json.dumps(attempts) and "secret" not in json.dumps(attempts)
    assert asyncio.run(run_once(factory, get_settings(), transport)) is False


def test_total_deadline_covers_slow_response():
    async def delayed(request):
        await asyncio.sleep(2)
        return httpx.Response(200)
    settings = Settings(_env_file=None, SECRET_KEY="x" * 32, DELIVERY_TIMEOUT_SECONDS=1)
    result = asyncio.run(send_event("https://hooks.example.com/x", "{}", "event", "job", settings, httpx.MockTransport(delayed)))
    assert result["error"] == "timeout" and result["duration_ms"] < 1800


def test_retry_limit_backoff_manual_retry_and_history(client, setup, monkeypatch):
    source, destination, factory = setup
    monkeypatch.setattr(get_settings(), "DELIVERY_MAX_ATTEMPTS", 2)
    event_id = ingest(client, source).json()["event_id"]
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    asyncio.run(run_once(factory, get_settings(), transport))
    first = job_for(client, event_id)
    with factory.begin() as db:
        job = db.get(DeliveryJob, first["id"])
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.job_id == job.id))
        assert (job.next_attempt_at - attempt.finished_at).total_seconds() == 5
        job.next_attempt_at = utcnow() - timedelta(seconds=1)
    asyncio.run(run_once(factory, get_settings(), transport))
    job = job_for(client, event_id)
    assert job["status"] == "failed" and job["attempts_total"] == 2
    assert asyncio.run(run_once(factory, get_settings(), transport)) is False
    client.patch(f'/api/destinations/{destination["id"]}', json={"enabled": False})
    assert client.post(f'/api/deliveries/{job["id"]}/retry').status_code == 409
    client.patch(f'/api/destinations/{destination["id"]}', json={"enabled": True})
    assert client.post(f'/api/deliveries/{job["id"]}/retry').status_code == 202
    assert client.post(f'/api/deliveries/{job["id"]}/retry').status_code == 409
    asyncio.run(run_once(factory, get_settings(), httpx.MockTransport(lambda request: httpx.Response(200))))
    final = job_for(client, event_id)
    assert final["status"] == "delivered" and final["attempts_total"] == 3 and final["cycle_attempts"] == 1
    assert len(client.get(f'/api/deliveries/{job["id"]}/attempts').json()) == 3


def test_atomic_claim_and_crash_recovery(client, setup):
    source, _, factory = setup
    event_id = ingest(client, source).json()["event_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim_job(factory, get_settings()), range(2)))
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    with factory.begin() as db:
        db.get(DeliveryJob, claim["id"]).lease_until = utcnow() - timedelta(seconds=1)
    recover_expired(factory, get_settings())
    finish_job(factory, claim, {"error": None, "status_code": 200, "duration_ms": 1, "response_metadata": {}}, get_settings())
    job = job_for(client, event_id)
    assert job["status"] == "queued" and job["attempts_total"] == 1
    assert client.get(f'/api/deliveries/{job["id"]}/attempts').json()[0]["error"] == "worker_interrupted"
    with factory.begin() as db:
        db.get(DeliveryJob, job["id"]).next_attempt_at = utcnow() - timedelta(seconds=1)
    restarted = sessionmaker(bind=factory.kw["bind"], expire_on_commit=False)
    asyncio.run(run_once(restarted, get_settings(), httpx.MockTransport(lambda request: httpx.Response(200))))
    assert job_for(client, event_id)["status"] == "delivered"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.100.100.200", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "2002:7f00:1::"])
def test_ssrf_blocked_ip_literals(ip):
    host = f"[{ip}]" if ":" in ip else ip
    with pytest.raises(UnsafeDestination):
        asyncio.run(resolve_destination(f"https://{host}/", get_settings()))


@pytest.mark.parametrize("url", ["http://example.com/hook", "file:///etc/passwd", "https://user:pass@example.com/hook", "https://example.com/?token=secret", "https://example.com/#secret", "https://example.com\\@127.0.0.1/", "https://example.com:99999", "https://[fe80::1%25eth0]/"])
def test_ssrf_invalid_urls(url):
    with pytest.raises(UnsafeDestination):
        parse_destination(url, get_settings())


def test_dns_mixed_answers_blocked_and_rechecked_before_send(client, setup, public_dns):
    source, _, factory = setup
    event_id = ingest(client, source).json()["event_id"]
    public_dns.return_value = [(2, 1, 6, "", (PUBLIC, 443)), (2, 1, 6, "", ("127.0.0.1", 443))]
    sent = []
    asyncio.run(run_once(factory, get_settings(), httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200))))
    assert sent == []
    job = job_for(client, event_id)
    assert client.get(f'/api/deliveries/{job["id"]}/attempts').json()[0]["error"] == "destination_blocked"
    assert client.post("/api/destinations", json={"source_id": source["id"], "name": "Unsafe", "url": "https://unsafe.example/hook"}).status_code == 422


def test_local_n8n_exception_is_development_only_and_exact():
    with pytest.raises(ValueError):
        Settings(_env_file=None, SECRET_KEY="x" * 32, DEV_ALLOWED_ORIGINS=["http://127.0.0.1:5678"])
    settings = Settings(_env_file=None, SECRET_KEY="x" * 32, ENVIRONMENT="development", DEV_ALLOWED_ORIGINS=["http://127.0.0.1:5678"])
    pinned, _, _ = asyncio.run(resolve_destination("http://127.0.0.1:5678/webhook/orders", settings))
    assert pinned.host == "127.0.0.1"
    with pytest.raises(UnsafeDestination):
        asyncio.run(resolve_destination("http://127.0.0.1:8000/", settings))
    with pytest.raises(ValueError):
        Settings(_env_file=None, SECRET_KEY="x" * 32, ENVIRONMENT="development", DEV_ALLOWED_ORIGINS=["http://169.254.169.254:80"])


@pytest.mark.parametrize("method,path,body", [("get", "/api/events", None), ("get", "/api/events/unknown", None), ("get", "/api/sources", None), ("post", "/api/sources", {"name": "x", "secret": SECRET}), ("get", "/api/destinations", None), ("get", "/api/stats", None), ("get", "/api/deliveries/unknown/attempts", None), ("post", "/api/deliveries/unknown/retry", None)])
def test_dashboard_requires_auth(client, method, path, body):
    assert client.request(method, path, json=body).status_code == 401


def test_owner_isolation(client, other_client, setup):
    source, destination, _ = setup
    event_id = ingest(client, source).json()["event_id"]
    job = job_for(client, event_id)
    sign_up_and_log_in(other_client, "bob@example.com")
    for path in ("/api/events", "/api/sources", "/api/destinations"):
        assert other_client.get(path).json() == []
    assert other_client.get("/api/stats").json()["events"] == 0
    assert other_client.get(f"/api/events/{event_id}").status_code == 404
    assert other_client.get(f'/api/deliveries/{job["id"]}/attempts').status_code == 404
    assert other_client.post(f'/api/deliveries/{job["id"]}/retry').status_code == 404
    assert other_client.patch(f'/api/destinations/{destination["id"]}', json={"enabled": False}).status_code == 404
    assert other_client.patch(f'/api/sources/{source["id"]}', json={"enabled": False}).status_code == 404
    assert other_client.post("/api/destinations", json={"source_id": source["id"], "name": "x", "url": "https://public.example/"}).status_code == 404


def test_validation_redaction_and_csrf(client, setup):
    source, _, _ = setup
    response = client.post("/api/sources", json={"name": "x", "secret": "sensitive-short-value"})
    assert response.status_code == 422 and "sensitive-short-value" not in response.text
    assert client.post("/api/sources", json={"name": "x", "secret": SECRET}, headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.get("/api/events").headers["cache-control"] == "no-store"
    assert client.patch(f'/api/sources/{source["id"]}', json={"enabled": True, "secret": "new-source-secret-with-more-than-32-characters"}).status_code == 200
    assert ingest(client, source).status_code == 401


@pytest.mark.parametrize("body", [b'{"value":"\\ud800"}', b'[' * 66 + b'0' + b']' * 66, b'{"value":1e999}'])
def test_json_safe_for_dashboard_serialization(client, setup, body):
    source, _, _ = setup
    assert client.post(f'/api/hooks/{source["id"]}', content=body, headers={"Content-Type": "application/json", "X-Webhook-Secret": SECRET}).status_code == 400


@pytest.mark.parametrize("ip", ["168.63.129.16", "64:ff9b::7f00:1"])
def test_platform_and_translation_addresses_blocked(ip):
    host = f"[{ip}]" if ":" in ip else ip
    with pytest.raises(UnsafeDestination):
        asyncio.run(resolve_destination(f"https://{host}/", get_settings()))


def test_concurrent_deduplication(client, setup):
    from fastapi.testclient import TestClient
    from main import app
    source, _, factory = setup
    def send(_):
        sender = TestClient(app)
        try:
            return ingest(sender, source, **{"Idempotency-Key": "racing-event"})
        finally:
            sender.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 202]
    assert len({response.json()["event_id"] for response in responses}) == 1
    with factory() as db:
        assert db.scalar(select(func.count(ReceivedEvent.id))) == 1
        assert db.scalar(select(func.count(DeliveryJob.id))) == 1


def test_initialization_preserves_existing_users_and_notes(engine, monkeypatch):
    import app.db as database
    from app.models import Note, User
    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        user = User(email="preserved@example.com", hashed_password="existing-hash")
        db.add(user)
        db.flush()
        db.add(Note(owner_id=user.id, title="Keep this note", body="existing user content"))
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    with factory() as db:
        assert db.scalar(select(User.email)) == "preserved@example.com"
        assert db.scalar(select(Note.body)) == "existing user content"


@pytest.mark.parametrize("status,state,error", [
    (200, "delivered", None), (299, "delivered", None),
    (400, "failed", "permanent_http_error"), (401, "failed", "permanent_http_error"),
    (403, "failed", "permanent_http_error"), (404, "failed", "permanent_http_error"),
    (405, "failed", "permanent_http_error"), (410, "failed", "permanent_http_error"),
    (422, "failed", "permanent_http_error"), (418, "failed", "permanent_http_error"),
    (408, "queued", "http_error"), (429, "queued", "http_error"),
    (500, "queued", "http_error"), (599, "queued", "http_error"),
    (301, "failed", "redirect_blocked"), (101, "failed", "unexpected_status"),
    (600, "failed", "unexpected_status"),
])
def test_http_retry_classification(client, setup, status, state, error):
    source, _, factory = setup
    event_id = ingest(client, source).json()["event_id"]
    sent = []
    transport = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(status))
    assert asyncio.run(run_once(factory, get_settings(), transport))
    job = job_for(client, event_id)
    assert (job["status"], job["attempts_total"], job["cycle_attempts"]) == (state, 1, 1)
    history = client.get(f'/api/deliveries/{job["id"]}/attempts').json()
    assert len(history) == 1 and history[0]["error"] == error
    if state != "queued":
        with factory.begin() as db:
            db.get(DeliveryJob, job["id"]).next_attempt_at = utcnow() - timedelta(seconds=1)
        assert not asyncio.run(run_once(factory, get_settings(), transport))
        assert len(sent) == 1


def test_edit_and_replay_permanent_failure_preserves_identity_history_and_dedup(client, setup):
    source, destination, factory = setup
    event_id = ingest(client, source, **{"Idempotency-Key": "permanent"}).json()["event_id"]
    asyncio.run(run_once(factory, get_settings(), httpx.MockTransport(lambda request: httpx.Response(404))))
    job = job_for(client, event_id)
    history_url = f'/api/deliveries/{job["id"]}/attempts'
    before = client.get(history_url).json()
    edited = client.patch(f'/api/destinations/{destination["id"]}', json={
        "name": "Corrected n8n", "url": "https://hooks.example.com/webhook/fixed", "enabled": False,
    })
    assert edited.status_code == 200
    assert edited.json()["id"] == destination["id"]
    assert edited.json()["source_id"] == source["id"]
    assert client.get(history_url).json() == before
    assert client.post(f'/api/deliveries/{job["id"]}/retry').status_code == 409
    client.patch(f'/api/destinations/{destination["id"]}', json={"enabled": True})
    assert client.post(f'/api/deliveries/{job["id"]}/retry').status_code == 202
    assert client.post(f'/api/deliveries/{job["id"]}/retry').status_code == 409
    def destination_handler(request):
        assert request.url.path == "/webhook/fixed"
        assert request.headers["Idempotency-Key"] == job["id"]
        return httpx.Response(200)
    asyncio.run(run_once(factory, get_settings(), httpx.MockTransport(destination_handler)))
    final = job_for(client, event_id)
    assert (final["id"], final["status"], final["attempts_total"], final["cycle_attempts"]) == (job["id"], "delivered", 2, 1)
    assert client.get(history_url).json()[1] == before[0]
    assert ingest(client, source, **{"Idempotency-Key": "permanent"}).status_code == 200
    with factory() as db:
        assert db.scalar(select(func.count(DeliveryJob.id))) == 1


@pytest.mark.parametrize("patch", [
    {}, {"name": None}, {"url": None}, {"enabled": None}, {"name": "   "},
    {"source_id": "other-source"}, {"name": "x" * 101},
    {"url": "https://127.0.0.1/private"}, {"url": "http://169.254.169.254/"},
    {"url": "https://user:private@public.example/"}, {"url": "https://public.example/?token=private"},
])
def test_destination_edit_rejects_invalid_changes_atomically(client, setup, patch):
    _, destination, _ = setup
    response = client.patch(f'/api/destinations/{destination["id"]}', json=patch)
    assert response.status_code == 422
    assert "private" not in response.text
    assert client.get("/api/destinations").json() == [destination]


def test_destination_edit_requires_owner_before_dns(client, other_client, setup, public_dns):
    _, destination, _ = setup
    path = f'/api/destinations/{destination["id"]}'
    assert other_client.patch(path, json={"name": "Changed"}).status_code == 401
    sign_up_and_log_in(other_client, "other@example.com")
    public_dns.reset_mock()
    assert other_client.patch(path, json={"url": "https://public.example/"}).status_code == 404
    public_dns.assert_not_called()
    assert client.get("/api/destinations").json() == [destination]


def test_destination_edit_claim_snapshot_and_local_n8n(client, setup, monkeypatch):
    source, destination, factory = setup
    event_id = ingest(client, source).json()["event_id"]
    claim = claim_job(factory, get_settings())
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "DEV_ALLOWED_ORIGINS", ["http://127.0.0.1:5678"])
    url = "http://127.0.0.1:5678/webhook/relayops-test"
    path = f'/api/destinations/{destination["id"]}'
    assert client.patch(path, json={"url": url}).status_code == 200
    assert claim["url"] == destination["url"]
    finish_job(factory, claim, {"error": "http_error", "status_code": 500, "duration_ms": 1, "response_metadata": {}}, settings)
    with factory.begin() as db:
        db.get(DeliveryJob, claim["id"]).next_attempt_at = utcnow() - timedelta(seconds=1)
    def local_handler(request):
        assert str(request.url) == url
        return httpx.Response(200)
    asyncio.run(run_once(factory, settings, httpx.MockTransport(local_handler)))
    assert job_for(client, event_id)["status"] == "delivered"
    assert client.patch(path, json={"url": "http://127.0.0.1:8000/"}).status_code == 422
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert client.patch(path, json={"url": url}).status_code == 422


def test_temporary_dns_failure_retries_without_sending(client, setup, public_dns):
    source, destination, factory = setup
    event_id = ingest(client, source).json()["event_id"]
    public_dns.side_effect = OSError("private resolver details")
    sent = []
    asyncio.run(run_once(factory, get_settings(), httpx.MockTransport(lambda request: sent.append(request))))
    job = job_for(client, event_id)
    assert job["status"] == "queued" and not sent
    response = client.get(f'/api/deliveries/{job["id"]}/attempts')
    assert response.json()[0]["error"] == "dns_error" and "private" not in response.text
    assert client.patch(f'/api/destinations/{destination["id"]}', json={"url": "https://unavailable.example/"}).status_code == 422


@pytest.mark.parametrize("failure_stage", ["claim", "finish"])
def test_transaction_failure_survives_restart(client, setup, monkeypatch, failure_stage):
    from app.db import make_engine
    source, _, factory = setup
    settings = get_settings()
    event_id = ingest(client, source).json()["event_id"]
    job_id = job_for(client, event_id)["id"]
    sent = []
    transport = httpx.MockTransport(lambda request: sent.append(
        (request.headers["Idempotency-Key"], request.headers["X-RelayOps-Event-ID"])
    ) or httpx.Response(204))
    commits = 0

    def fail_commit(db):
        nonlocal commits
        commits += 1
        # run_once commits recovery, claim, then completion.
        if commits == (2 if failure_stage == "claim" else 3):
            db.flush()
            raise RuntimeError("injected commit failure")

    sql_event.listen(factory, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="injected commit failure"):
            asyncio.run(run_once(factory, settings, transport))
    finally:
        sql_event.remove(factory, "before_commit", fail_commit)
    with factory() as db:
        job = db.get(DeliveryJob, job_id)
        assert job.status == ("queued" if failure_stage == "claim" else "running")
        assert job.attempts_total == (0 if failure_stage == "claim" else 1)
        attempts = db.scalars(select(DeliveryAttempt)).all()
        assert len(attempts) == job.attempts_total
        assert all(a.finished_at is None for a in attempts)
        recovery_time = (job.lease_until.replace(tzinfo=utcnow().tzinfo) + timedelta(seconds=1)
                         if job.lease_until else utcnow())
    # New engine/session models a restart against the same persisted file.
    restarted_engine = make_engine(str(factory.kw["bind"].url))
    restarted = sessionmaker(bind=restarted_engine, expire_on_commit=False)
    try:
        monkeypatch.setattr("app.delivery.utcnow", lambda: recovery_time)
        recover_expired(restarted, settings)
        monkeypatch.setattr("app.delivery.utcnow", lambda: recovery_time + timedelta(seconds=5))
        assert asyncio.run(run_once(restarted, settings, transport))
        assert not asyncio.run(run_once(restarted, settings, transport))
        with restarted() as db:
            job = db.get(DeliveryJob, job_id)
            assert job.status == "delivered"
            assert job.claim_token is None and job.lease_until is None
            attempts = db.scalars(select(DeliveryAttempt).order_by(DeliveryAttempt.number)).all()
            assert len(attempts) == (1 if failure_stage == "claim" else 2)
            assert all(a.finished_at is not None for a in attempts)
            if failure_stage == "finish":
                assert attempts[0].error == "worker_interrupted"
            assert attempts[-1].status_code == 204
            assert db.scalar(select(func.count(DeliveryJob.id))) == 1
        assert sent == [(job_id, event_id)] * (1 if failure_stage == "claim" else 2)
    finally:
        restarted_engine.dispose()


def test_completion_loses_ownership_between_read_and_write(client, setup):
    source, _, factory = setup
    event_id = ingest(client, source).json()["event_id"]
    claim = claim_job(factory, get_settings())
    with factory.begin() as db:
        db.get(DeliveryJob, claim["id"]).lease_until = utcnow() - timedelta(seconds=1)
    engine = factory.kw["bind"]
    intervened = False

    def recover_before_write(conn, cursor, statement, parameters, context, executemany):
        nonlocal intervened
        if not intervened and statement.startswith("UPDATE relay_jobs"):
            intervened = True
            recover_expired(factory, get_settings())

    sql_event.listen(engine, "before_cursor_execute", recover_before_write)
    try:
        finish_job(factory, claim, {"error": None, "status_code": 204,
                                   "duration_ms": 1, "response_metadata": {}}, get_settings())
    finally:
        sql_event.remove(engine, "before_cursor_execute", recover_before_write)
    assert intervened
    assert job_for(client, event_id)["status"] == "queued"
    with factory() as db:
        attempt = db.get(DeliveryAttempt, claim["attempt_id"])
        assert attempt.error == "worker_interrupted" and attempt.status_code is None


def test_interrupted_attempt_exhaustion_and_replay(client, setup, monkeypatch):
    source, _, factory = setup
    monkeypatch.setattr(get_settings(), "DELIVERY_MAX_ATTEMPTS", 1)
    event_id = ingest(client, source).json()["event_id"]
    claim = claim_job(factory, get_settings())
    recover_expired(factory, get_settings())
    assert job_for(client, event_id)["status"] == "running"  # Lease still valid.
    with factory.begin() as db:
        db.get(DeliveryJob, claim["id"]).lease_until = utcnow() - timedelta(seconds=1)
    recover_expired(factory, get_settings())
    recover_expired(factory, get_settings())
    assert job_for(client, event_id)["status"] == "failed"
    assert claim_job(factory, get_settings()) is None
    assert client.post(f'/api/deliveries/{claim["id"]}/retry').status_code == 202
    assert asyncio.run(run_once(factory, get_settings(), httpx.MockTransport(lambda r: httpx.Response(204))))
    job = job_for(client, event_id)
    assert (job["status"], job["attempts_total"], job["cycle_attempts"]) == ("delivered", 2, 1)
    history = client.get(f'/api/deliveries/{claim["id"]}/attempts').json()
    assert len(history) == 2 and history[1]["error"] == "worker_interrupted"
