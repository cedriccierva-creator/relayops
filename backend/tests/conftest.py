# Test setup: safe env vars, a fresh temp SQLite DB per test, and two independent clients.
import os
import tempfile

# Must be set before the app is imported. Real env vars override backend/.env.
os.environ["SECRET_KEY"] = "test-secret-key-not-for-real-use-0123456789abcdef"
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/import-time.db"
os.environ["REGISTRATION_ENABLED"] = "true"
# Tests must not inherit local n8n or other deployment configuration.
os.environ["ENVIRONMENT"] = "production"
os.environ["DEV_ALLOWED_ORIGINS"] = "[]"
os.environ["WEBHOOK_RATE_LIMIT"] = "60"
os.environ["WEBHOOK_RATE_WINDOW_SECONDS"] = "60"
os.environ["WEBHOOK_RATE_MAX_SOURCES"] = "10000"
os.environ["RETENTION_ENABLED"] = "false"
os.environ["RETENTION_DAYS"] = "30"
os.environ["RETENTION_BATCH_SIZE"] = "100"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db, make_engine
from main import app as fastapi_app
from app.ingestion_limits import get_ingestion_limiter


@pytest.fixture(autouse=True)
def fresh_ingestion_limiter():
    get_ingestion_limiter.cache_clear()
    yield
    get_ingestion_limiter.cache_clear()


@pytest.fixture()
def engine(tmp_path):
    test_engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(test_engine)
    yield test_engine
    test_engine.dispose()


@pytest.fixture()
def client(engine):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    yield TestClient(fastapi_app)
    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def other_client(client):
    """A second browser: same app and database, separate cookie jar."""
    return TestClient(fastapi_app)


def sign_up_and_log_in(c, email="alice@example.com", password="correct-horse-battery"):
    assert c.post("/api/auth/register", json={"email": email, "password": password}).status_code == 201
    response = c.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response
