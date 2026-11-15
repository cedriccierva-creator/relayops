# Database tables: User and Note (each note belongs to one user).
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text, JSON, UniqueConstraint, Index
from uuid import uuid4
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # always stored lowercase
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def new_id() -> str:
    return str(uuid4())


class WebhookSource(Base):
    __tablename__ = "relay_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    secret_hash: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReceivedEvent(Base):
    __tablename__ = "relay_events"
    __table_args__ = (UniqueConstraint("source_id", "idempotency_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("relay_sources.id"), index=True)
    idempotency_hash: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int]
    request_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Destination(Base):
    __tablename__ = "relay_destinations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("relay_sources.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(2048))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryJob(Base):
    __tablename__ = "relay_jobs"
    __table_args__ = (UniqueConstraint("event_id", "destination_id"), Index("ix_relay_jobs_due", "status", "next_attempt_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("relay_events.id"), index=True)
    destination_id: Mapped[str] = mapped_column(ForeignKey("relay_destinations.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts_total: Mapped[int] = mapped_column(default=0)
    cycle_attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int]
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryAttempt(Base):
    __tablename__ = "relay_attempts"
    __table_args__ = (UniqueConstraint("job_id", "number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("relay_jobs.id"), index=True)
    number: Mapped[int]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int | None]
    response_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
