import asyncio
import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.ingestion_limits import get_ingestion_limiter
from app.models import DeliveryJob, Destination, ReceivedEvent, WebhookSource

router = APIRouter(prefix="/api/hooks", tags=["webhooks"])


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def reject_constant(_value):
    raise ValueError("Non-finite JSON number")


def validate_json_tree(payload):
    # Keep downstream serialization/redaction bounded and reject lone UTF-16 surrogates.
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64 levels")
        if isinstance(value, str):
            value.encode("utf-8")
        elif isinstance(value, dict):
            for key, item in value.items():
                key.encode("utf-8")
                pending.append((item, depth + 1))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


@router.post("/{source_id}", status_code=202)
async def ingest(source_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
    settings = get_settings()
    source = db.get(WebhookSource, source_id)
    supplied = request.headers.get("x-webhook-secret", "")
    if (not source or not source.enabled or not supplied or len(supplied) > 256
            or not hmac.compare_digest(source.secret_hash, secret_hash(supplied))):
        raise HTTPException(401, "Invalid webhook credentials")
    retry_after = get_ingestion_limiter(
        settings.WEBHOOK_RATE_LIMIT, settings.WEBHOOK_RATE_WINDOW_SECONDS,
        settings.WEBHOOK_RATE_MAX_SOURCES,
    ).retry_after(source.id)
    if retry_after:
        raise HTTPException(429, "Webhook rate limit exceeded", headers={"Retry-After": str(retry_after)})
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "Compressed request bodies are not supported")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared_size = int(length)
            if declared_size < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length") from None
        if declared_size > settings.WEBHOOK_MAX_BYTES:
            raise HTTPException(413, "Webhook payload is too large")
    body = bytearray()
    try:
        async with asyncio.timeout(settings.WEBHOOK_READ_TIMEOUT):
            async for chunk in request.stream():
                if len(body) + len(chunk) > settings.WEBHOOK_MAX_BYTES:
                    raise HTTPException(413, "Webhook payload is too large")
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "Webhook body read timed out") from None
    try:
        raw = body.decode("utf-8")
        payload = json.loads(raw, parse_constant=reject_constant)
        validate_json_tree(payload)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "Invalid UTF-8 JSON payload") from None
    key = request.headers.get("idempotency-key")
    if key is None:
        key = request.headers.get("x-event-id")
    if key is not None and (not key.strip() or len(key) > 256):
        raise HTTPException(400, "Idempotency key must contain 1–256 characters")
    key_hash = secret_hash(key) if key is not None else None
    payload_hash = secret_hash(canonical)

    def duplicate(existing):
        if existing.payload_hash != payload_hash:
            raise HTTPException(409, "Idempotency key already used for a different payload")
        response.status_code = 200
        return {"event_id": existing.id, "duplicate": True}

    if key_hash:
        existing = db.scalar(select(ReceivedEvent).where(
            ReceivedEvent.source_id == source.id, ReceivedEvent.idempotency_hash == key_hash,
        ))
        if existing:
            return duplicate(existing)
    event = ReceivedEvent(
        source_id=source.id, payload=raw, payload_hash=payload_hash, idempotency_hash=key_hash,
        size_bytes=len(body), request_metadata={"content_type": "application/json", "has_idempotency_key": key is not None},
    )
    db.add(event)
    try:
        db.flush()
        destinations = db.scalars(select(Destination).where(Destination.source_id == source.id, Destination.enabled.is_(True))).all()
        db.add_all([DeliveryJob(event_id=event.id, destination_id=dest.id, max_attempts=settings.DELIVERY_MAX_ATTEMPTS) for dest in destinations])
        db.commit()  # event + fan-out commit together, before acknowledging receipt
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(ReceivedEvent).where(
            ReceivedEvent.source_id == source_id, ReceivedEvent.idempotency_hash == key_hash,
        )) if key_hash else None
        if existing:
            return duplicate(existing)
        raise
    return {"event_id": event.id, "duplicate": False}
