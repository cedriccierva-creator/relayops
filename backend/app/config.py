# Settings loaded from backend/.env; refuses to start on a missing or weak SECRET_KEY.
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # the backend/ folder
PLACEHOLDER_SECRET = "change-me-generate-a-real-key-before-running"  # same value as .env.example


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore", hide_input_in_errors=True)

    SECRET_KEY: str  # no default on purpose: the app must not start without a real key
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    COOKIE_SECURE: bool = False  # set true when serving over HTTPS
    ENVIRONMENT: str = "production"
    REGISTRATION_ENABLED: bool = False  # Explicit opt-in, including development.
    WEBHOOK_MAX_BYTES: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    WEBHOOK_READ_TIMEOUT: float = Field(default=10, ge=1, le=60)
    WEBHOOK_RATE_LIMIT: int = Field(default=60, ge=1, le=100_000)
    WEBHOOK_RATE_WINDOW_SECONDS: int = Field(default=60, ge=1, le=3600)
    WEBHOOK_RATE_MAX_SOURCES: int = Field(default=10_000, ge=1, le=100_000)
    RETENTION_ENABLED: bool = False  # Enables planning only; no payload deletion.
    RETENTION_DAYS: int = Field(default=30, ge=1, le=36_500)
    RETENTION_BATCH_SIZE: int = Field(default=100, ge=1, le=1000)
    DELIVERY_TIMEOUT_SECONDS: float = Field(default=15, ge=1, le=60)
    WORKER_HEALTH_MAX_AGE_SECONDS: int = Field(default=120, ge=15, le=3600)
    DELIVERY_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=10)
    RETRY_BASE_SECONDS: int = Field(default=5, ge=1, le=300)
    # Exact origins only; even in development, only loopback may be exempted.
    DEV_ALLOWED_ORIGINS: list[str] = Field(default_factory=list)
    DASHBOARD_ORIGINS: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    @model_validator(mode="after")
    def restrict_development_exceptions(self):
        from urllib.parse import urlsplit
        import ipaddress

        if self.ENVIRONMENT not in {"production", "development"}:
            raise ValueError("ENVIRONMENT must be production or development")
        if self.DEV_ALLOWED_ORIGINS and self.ENVIRONMENT != "development":
            raise ValueError("Local destinations require ENVIRONMENT=development")
        for origin in self.DEV_ALLOWED_ORIGINS:
            parsed = urlsplit(origin)
            try:
                local = ipaddress.ip_address(parsed.hostname or "").is_loopback
            except ValueError:
                local = parsed.hostname == "localhost"
            if (not local or parsed.scheme not in {"http", "https"}
                    or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment or not parsed.port):
                raise ValueError("Development origins must be exact loopback origins with a port")
        return self

    @field_validator("SECRET_KEY")
    @classmethod
    def secret_must_be_real(cls, value: str) -> str:
        if value == PLACEHOLDER_SECRET:
            raise ValueError("SECRET_KEY is still the placeholder from .env.example; generate a real one")
        if len(value) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
