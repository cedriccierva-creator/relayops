# Auth tests: register, login, cookie flags, /me, logout, tampered/expired/inactive tokens.
from sqlalchemy import text

from app.security import create_access_token
from tests.conftest import sign_up_and_log_in

PASSWORD = "correct-horse-battery"


def test_register_ok_lowercases_email_and_hides_hash(client):
    response = client.post("/api/auth/register", json={"email": "Alice@Example.com", "password": PASSWORD})
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert "hashed_password" not in body and "password" not in body


def test_register_stores_argon2_hash(client, engine):
    client.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD})
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT hashed_password FROM users")).scalar_one()
    assert stored.startswith("$argon2") and PASSWORD not in stored


def test_register_duplicate_is_409_even_with_different_case(client):
    client.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD})
    response = client.post("/api/auth/register", json={"email": "A@EXAMPLE.COM", "password": PASSWORD})
    assert response.status_code == 409


def test_register_validation(client):
    assert client.post("/api/auth/register", json={"email": "a@example.com", "password": "short"}).status_code == 422
    assert client.post("/api/auth/register", json={"email": "a@example.com", "password": "x" * 129}).status_code == 422
    assert client.post("/api/auth/register", json={"email": "not-an-email", "password": PASSWORD}).status_code == 422


def test_login_sets_httponly_cookie(client):
    response = sign_up_and_log_in(client)
    cookie_header = response.headers["set-cookie"].lower()
    assert "access_token=" in cookie_header
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header
    assert "path=/" in cookie_header
    assert "max-age=3600" in cookie_header
    assert "secure" not in cookie_header.replace("samesite", "")  # COOKIE_SECURE defaults to false


def test_login_failures_look_identical(client):
    client.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD})
    wrong_password = client.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
    unknown_email = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"detail": "Invalid credentials"}
    assert "access_token" not in client.cookies


def test_me_requires_cookie(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_with_cookie(client):
    sign_up_and_log_in(client)
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["email"] == "alice@example.com"


def test_me_rejects_tampered_garbage_and_expired_tokens(client):
    sign_up_and_log_in(client)
    good = client.cookies["access_token"]
    for bad in (good[:-2] + "xx", "not-a-jwt", create_access_token(1, expires_minutes=-5)):
        client.cookies.clear()
        client.cookies.set("access_token", bad)
        assert client.get("/api/auth/me").status_code == 401


def test_inactive_user_token_is_rejected(client, engine):
    sign_up_and_log_in(client)
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = 0"))
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 401


def test_logout_clears_cookie(client):
    sign_up_and_log_in(client)
    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").status_code == 401


def test_registration_disabled_preserves_login_and_sessions(client, engine, monkeypatch):
    from app.config import get_settings
    sign_up_and_log_in(client)
    monkeypatch.setattr(get_settings(), "REGISTRATION_ENABLED", False)
    assert client.get("/api/auth/policy").json() == {"registration_enabled": False}
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    for email in ("alice@example.com", "new@example.com"):
        response = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
        assert response.status_code == 403 and "disabled" in response.json()["detail"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 1
    assert client.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 200
    monkeypatch.setattr(get_settings(), "REGISTRATION_ENABLED", True)
    assert client.get("/api/auth/policy").json() == {"registration_enabled": True}
    assert client.post("/api/auth/register", json={"email": "new@example.com", "password": PASSWORD}).status_code == 201
