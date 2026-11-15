# Config tests (bad SECRET_KEY refuses to start) plus the health endpoint.
import pytest
from pydantic import ValidationError

from app.config import PLACEHOLDER_SECRET, Settings


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_secret_key_refuses_to_start(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("bad", ["", "too-short", "x" * 31, PLACEHOLDER_SECRET])
def test_weak_or_placeholder_secret_key_refuses_to_start(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SECRET_KEY=bad)


def test_good_secret_key_is_accepted():
    assert Settings(_env_file=None, SECRET_KEY="x" * 32).ACCESS_TOKEN_EXPIRE_MINUTES == 60


@pytest.mark.parametrize("environment", ["production", "development"])
def test_registration_requires_explicit_opt_in(environment, monkeypatch):
    monkeypatch.delenv("REGISTRATION_ENABLED", raising=False)
    assert Settings(_env_file=None, ENVIRONMENT=environment).REGISTRATION_ENABLED is False
    assert Settings(_env_file=None, ENVIRONMENT=environment, REGISTRATION_ENABLED=True).REGISTRATION_ENABLED is True
