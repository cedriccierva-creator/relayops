# Pydantic request/response shapes; hashed_password never appears in any of them.
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


def _as_utc(value: datetime) -> datetime:
    """SQLite drops timezones; re-attach UTC so JSON times end in +00:00 and browsers parse them correctly."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, value: str) -> str:
        return value.strip().lower()


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    created_at: datetime

    _utc = field_validator("created_at")(_as_utc)


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=10_000)


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    body: str
    created_at: datetime

    _utc = field_validator("created_at")(_as_utc)


# RelayOps never serializes credentials, URL query strings, or stored secret hashes.
from typing import Any
from pydantic import SecretStr


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    secret: SecretStr = Field(min_length=32, max_length=256)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Name must not be blank")
        return value.strip()


class SourceUpdate(BaseModel):
    enabled: bool
    secret: SecretStr | None = Field(default=None, min_length=32, max_length=256)


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    enabled: bool
    created_at: datetime
    _utc = field_validator("created_at")(_as_utc)


class DestinationCreate(BaseModel):
    source_id: str
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)
    _name = field_validator("name")(SourceCreate.nonempty_name.__func__)


class DestinationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None
    _name = field_validator("name")(SourceCreate.nonempty_name.__func__)

    @model_validator(mode="before")
    @classmethod
    def nonempty_patch(cls, value):
        if isinstance(value, dict) and (not value or any(item is None for item in value.values())):
            raise ValueError("Supply at least one setting; null values are not allowed")
        return value


class DestinationOut(SourceOut):
    source_id: str
    url: str


class EventOut(BaseModel):
    id: str
    source_id: str
    source_name: str
    received_at: datetime
    size_bytes: int
    status: str
    delivery_count: int
    _utc = field_validator("received_at")(_as_utc)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    event_id: str
    destination_id: str
    status: str
    attempts_total: int
    cycle_attempts: int
    max_attempts: int
    next_attempt_at: datetime
    _utc = field_validator("next_attempt_at")(_as_utc)


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    job_id: str
    number: int
    started_at: datetime
    finished_at: datetime | None
    status_code: int | None
    error: str | None
    duration_ms: int | None
    response_metadata: dict
    _utc = field_validator("started_at")(_as_utc)

    @field_validator("finished_at")
    @classmethod
    def optional_utc(cls, value):
        return _as_utc(value) if value else None


class EventDetail(EventOut):
    payload: Any
    request_metadata: dict
    jobs: list[JobOut]
