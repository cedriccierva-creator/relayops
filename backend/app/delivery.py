"""Durable, leased delivery jobs. The worker entrypoint also holds a host-local lock."""
import asyncio
import logging
import time
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select, update

from app.config import Settings
from app.models import DeliveryAttempt, DeliveryJob, Destination, ReceivedEvent, utcnow
from app.url_security import DestinationResolutionError, UnsafeDestination, resolve_destination

logger = logging.getLogger(__name__)

def http_failure(status):
    if 200 <= status < 300:
        return None
    if status in {408, 429} or 500 <= status < 600:
        return "http_error"
    if 400 <= status < 500:
        return "permanent_http_error"
    return "redirect_blocked" if 300 <= status < 400 else "unexpected_status"


def retryable(outcome):
    # Unknown failures fail closed; only explicitly transient categories retry.
    if outcome["status_code"] is not None:
        return http_failure(outcome["status_code"]) == "http_error"
    return outcome["error"] in {"timeout", "connection_error", "dns_error", "transport_error"}


async def send_event(url: str, payload: str, event_id: str, job_id: str, settings: Settings, transport=None):
    started = time.monotonic()
    status = None
    metadata = {}
    error = None
    try:
        # Includes DNS, connection, TLS, upload and response headers. Response bodies are
        # deliberately not read or persisted: destinations may reflect credentials.
        async with asyncio.timeout(settings.DELIVERY_TIMEOUT_SECONDS):
            pinned, host, server_name = await resolve_destination(url, settings)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.DELIVERY_TIMEOUT_SECONDS, connect=min(5, settings.DELIVERY_TIMEOUT_SECONDS)),
                follow_redirects=False, trust_env=False, transport=transport,
            ) as client:
                async with client.stream("POST", pinned, content=payload.encode("utf-8"), headers={
                    "Host": host, "Content-Type": "application/json", "User-Agent": "RelayOps/1.0",
                    "Idempotency-Key": job_id, "X-RelayOps-Event-ID": event_id,
                }, extensions={"sni_hostname": server_name}) as response:
                    status = response.status_code
                    # Normalize rather than retaining arbitrary, untrusted header values.
                    content_type = response.headers.get("content-type", "").split(";")[0].lower()
                    if content_type in {"application/json", "text/plain", "text/html", "application/xml"}:
                        metadata["content_type"] = content_type
                    error = http_failure(status)
    except DestinationResolutionError:
        error = "dns_error"
    except UnsafeDestination:
        error = "destination_blocked"
    except (TimeoutError, httpx.TimeoutException):
        error = "timeout"
    except httpx.HTTPError:
        error = "connection_error"
    except (ValueError, OSError):
        error = "transport_error"
    return {"status_code": status, "error": error, "duration_ms": int((time.monotonic() - started) * 1000),
            "response_metadata": metadata}


def backoff(settings, cycle_attempts):
    return min(3600, settings.RETRY_BASE_SECONDS * 2 ** max(0, cycle_attempts - 1))


def recover_expired(factory, settings):
    now = utcnow()
    with factory.begin() as db:
        expired = db.scalars(select(DeliveryJob).where(DeliveryJob.status == "running", DeliveryJob.lease_until <= now)).all()
        for job in expired:
            result = db.execute(update(DeliveryJob).where(
                DeliveryJob.id == job.id, DeliveryJob.status == "running", DeliveryJob.claim_token == job.claim_token,
                DeliveryJob.lease_until <= now,
            ).execution_options(synchronize_session=False).values(status="failed" if job.cycle_attempts >= job.max_attempts else "queued", claim_token=None,
                     lease_until=None, next_attempt_at=now + timedelta(seconds=backoff(settings, job.cycle_attempts))))
            if result.rowcount:
                db.execute(update(DeliveryAttempt).where(DeliveryAttempt.job_id == job.id, DeliveryAttempt.finished_at.is_(None)).values(
                    finished_at=now, error="worker_interrupted",
                ))


def claim_job(factory, settings):
    now = utcnow()
    token = str(uuid4())
    with factory.begin() as db:
        candidate = select(DeliveryJob.id).join(Destination).where(
            DeliveryJob.status == "queued", DeliveryJob.next_attempt_at <= now,
            DeliveryJob.cycle_attempts < DeliveryJob.max_attempts, Destination.enabled.is_(True),
        ).order_by(DeliveryJob.next_attempt_at, DeliveryJob.id).limit(1).scalar_subquery()
        # Selection and state transition are ONE SQL statement, so two workers cannot
        # both select an unclaimed row then overwrite each other's claim.
        job = db.scalar(update(DeliveryJob).where(DeliveryJob.id == candidate, DeliveryJob.status == "queued").values(
            status="running", claim_token=token,
            lease_until=now + timedelta(seconds=settings.DELIVERY_TIMEOUT_SECONDS + 60),
            attempts_total=DeliveryJob.attempts_total + 1, cycle_attempts=DeliveryJob.cycle_attempts + 1,
        ).returning(DeliveryJob))
        if job is None:
            return None
        attempt = DeliveryAttempt(job_id=job.id, number=job.attempts_total, started_at=now)
        db.add(attempt)
        db.flush()
        event = db.get(ReceivedEvent, job.event_id)
        destination = db.get(Destination, job.destination_id)
        return {"id": job.id, "token": token, "attempt_id": attempt.id, "url": destination.url,
                "event_id": event.id, "payload": event.payload}


def finish_job(factory, claim, outcome, settings):
    now = utcnow()
    with factory.begin() as db:
        job = db.scalar(select(DeliveryJob).where(
            DeliveryJob.id == claim["id"], DeliveryJob.claim_token == claim["token"], DeliveryJob.status == "running",
        ))
        if job is None:
            return  # A stale claimant must never overwrite a recovered job.
        status = ("delivered" if outcome["error"] is None else
                  "failed" if not retryable(outcome) or job.cycle_attempts >= job.max_attempts else "queued")
        # Ownership can change after the read. Fence the write itself, and commit
        # the job and attempt together so a failed transaction remains recoverable.
        result = db.execute(update(DeliveryJob).where(
            DeliveryJob.id == claim["id"], DeliveryJob.claim_token == claim["token"],
            DeliveryJob.status == "running",
        ).execution_options(synchronize_session=False).values(
            status=status, next_attempt_at=now + timedelta(seconds=backoff(settings, job.cycle_attempts)),
            lease_until=None, claim_token=None,
        ))
        if result.rowcount != 1:
            return
        db.execute(update(DeliveryAttempt).where(DeliveryAttempt.id == claim["attempt_id"]).values(finished_at=now, **outcome))
        number = job.attempts_total
    # Log only after commit, using identifiers and classifications, never transport text.
    failure = "none" if outcome["error"] is None else "transient" if retryable(outcome) else "permanent"
    safe_errors = {None, "http_error", "permanent_http_error", "redirect_blocked", "unexpected_status",
                   "dns_error", "destination_blocked", "timeout", "connection_error", "transport_error"}
    error = outcome["error"] if outcome["error"] in safe_errors else "unknown"
    logger.info("Delivery event_id=%s delivery_id=%s attempt=%s http_status=%s error=%s failure=%s result=%s",
                claim["event_id"], claim["id"], number, outcome["status_code"], error, failure,
                "retry_scheduled" if status == "queued" else status)


async def run_once(factory, settings, transport=None):
    recover_expired(factory, settings)
    claim = claim_job(factory, settings)
    if claim is None:
        return False
    outcome = await send_event(claim["url"], claim["payload"], claim["event_id"], claim["id"], settings, transport)
    finish_job(factory, claim, outcome, settings)
    return True
