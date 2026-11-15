import json
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models import DeliveryAttempt, DeliveryJob, Destination, ReceivedEvent, User, WebhookSource, utcnow
from app.redaction import redact
from app.routers.hooks import secret_hash
from app.schemas import (AttemptOut, DestinationCreate, DestinationOut, DestinationUpdate,
                         EventDetail, EventOut, JobOut, SourceCreate, SourceOut, SourceUpdate)
from app.url_security import UnsafeDestination, validate_destination

router = APIRouter(prefix="/api", tags=["relayops"])


def owned_source(db, source_id, user):
    source = db.scalar(select(WebhookSource).where(WebhookSource.id == source_id, WebhookSource.owner_id == user.id))
    if source is None:
        raise HTTPException(404, "Source not found")
    return source


def owned_job(db, job_id, user):
    job = db.scalar(select(DeliveryJob).join(ReceivedEvent).join(WebhookSource).where(
        DeliveryJob.id == job_id, WebhookSource.owner_id == user.id,
    ))
    if job is None:
        raise HTTPException(404, "Delivery not found")
    return job


def event_summary(event, source_name, jobs):
    states = {job.status for job in jobs}
    status = ("unrouted" if not jobs else "failed" if "failed" in states else
              "running" if "running" in states else "queued" if "queued" in states else "delivered")
    return dict(id=event.id, source_id=event.source_id, source_name=source_name,
                received_at=event.received_at, size_bytes=event.size_bytes,
                status=status, delivery_count=len(jobs))


@router.get("/sources", response_model=list[SourceOut])
def sources(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(WebhookSource).where(WebhookSource.owner_id == user.id).order_by(WebhookSource.created_at.desc())).all()


@router.post("/sources", response_model=SourceOut, status_code=201)
def create_source(payload: SourceCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    source = WebhookSource(owner_id=user.id, name=payload.name, secret_hash=secret_hash(payload.secret.get_secret_value()))
    db.add(source)
    db.commit()
    return source


@router.patch("/sources/{source_id}", response_model=SourceOut)
def update_source(source_id: str, payload: SourceUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    source = owned_source(db, source_id, user)
    source.enabled = payload.enabled
    if payload.secret is not None:
        source.secret_hash = secret_hash(payload.secret.get_secret_value())
    db.commit()
    return source


@router.get("/destinations", response_model=list[DestinationOut])
def destinations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Destination).join(WebhookSource).where(WebhookSource.owner_id == user.id).order_by(Destination.created_at.desc())).all()


@router.post("/destinations", response_model=DestinationOut, status_code=201)
async def create_destination(payload: DestinationCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_source(db, payload.source_id, user)
    try:
        await validate_destination(payload.url, get_settings())
    except UnsafeDestination as exc:
        raise HTTPException(422, str(exc)) from None
    destination = Destination(source_id=payload.source_id, name=payload.name, url=payload.url)
    db.add(destination)
    db.commit()
    return destination


@router.patch("/destinations/{destination_id}", response_model=DestinationOut)
async def update_destination(destination_id: str, payload: DestinationUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    destination = db.scalar(select(Destination).join(WebhookSource).where(
        Destination.id == destination_id, WebhookSource.owner_id == user.id,
    ))
    if destination is None:
        raise HTTPException(404, "Destination not found")
    if payload.url is not None:
        try:
            await validate_destination(payload.url, get_settings())
        except UnsafeDestination as exc:
            raise HTTPException(422, str(exc)) from None
    # A claimed attempt has already captured its URL. Queued/replayed attempts
    # read the latest settings when claimed; historical attempts are untouched.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(destination, field, value)
    db.commit()
    return destination


@router.get("/events", response_model=list[EventOut])
def events(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(ReceivedEvent, WebhookSource.name).join(WebhookSource).where(
        WebhookSource.owner_id == user.id,
    ).order_by(ReceivedEvent.received_at.desc(), ReceivedEvent.id.desc()).limit(limit).offset(offset)).all()
    jobs = defaultdict(list)
    if rows:
        for job in db.scalars(select(DeliveryJob).where(DeliveryJob.event_id.in_([event.id for event, _ in rows]))):
            jobs[job.event_id].append(job)
    return [event_summary(event, name, jobs[event.id]) for event, name in rows]


@router.get("/events/{event_id}", response_model=EventDetail)
def event_detail(event_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.execute(select(ReceivedEvent, WebhookSource.name).join(WebhookSource).where(
        ReceivedEvent.id == event_id, WebhookSource.owner_id == user.id,
    )).first()
    if row is None:
        raise HTTPException(404, "Event not found")
    event, name = row
    jobs = db.scalars(select(DeliveryJob).where(DeliveryJob.event_id == event.id).order_by(DeliveryJob.created_at)).all()
    return {**event_summary(event, name, jobs), "payload": redact(json.loads(event.payload)),
            "request_metadata": event.request_metadata, "jobs": jobs}


@router.get("/deliveries/{job_id}/attempts", response_model=list[AttemptOut])
def attempts(job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_job(db, job_id, user)
    return db.scalars(select(DeliveryAttempt).where(DeliveryAttempt.job_id == job_id).order_by(DeliveryAttempt.number.desc())).all()


@router.post("/deliveries/{job_id}/retry", response_model=JobOut, status_code=202)
def retry(job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = owned_job(db, job_id, user)
    if not db.get(Destination, job.destination_id).enabled:
        raise HTTPException(409, "Enable the destination before retrying")
    result = db.execute(update(DeliveryJob).where(DeliveryJob.id == job_id, DeliveryJob.status == "failed").values(
        status="queued", cycle_attempts=0, next_attempt_at=utcnow(), claim_token=None, lease_until=None,
    ))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Only failed deliveries can be retried")
    db.commit()
    db.refresh(job)
    return job


@router.get("/stats")
def stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    event_count = db.scalar(select(func.count(ReceivedEvent.id)).join(WebhookSource).where(WebhookSource.owner_id == user.id))
    rows = db.execute(select(DeliveryJob.status, func.count(DeliveryJob.id)).join(ReceivedEvent).join(WebhookSource).where(
        WebhookSource.owner_id == user.id,
    ).group_by(DeliveryJob.status)).all()
    counts = {"queued": 0, "running": 0, "delivered": 0, "failed": 0, **dict(rows)}
    total = sum(counts.values())
    return {"events": event_count, "deliveries": total, **counts,
            "success_rate": round(counts["delivered"] / total * 100, 1) if total else None}
