# Notes tests: auth required, create/list/delete, and strict per-user isolation.
from tests.conftest import sign_up_and_log_in


def test_notes_require_auth(client):
    assert client.get("/api/notes").status_code == 401
    assert client.post("/api/notes", json={"title": "x"}).status_code == 401
    assert client.delete("/api/notes/1").status_code == 401


def test_create_list_delete(client):
    sign_up_and_log_in(client)
    created = client.post("/api/notes", json={"title": "First", "body": "hello"})
    assert created.status_code == 201
    note_id = created.json()["id"]
    assert client.get("/api/notes").json()[0]["title"] == "First"
    assert client.delete(f"/api/notes/{note_id}").status_code == 204
    assert client.get("/api/notes").json() == []
    assert client.delete(f"/api/notes/{note_id}").status_code == 404


def test_note_validation(client):
    sign_up_and_log_in(client)
    assert client.post("/api/notes", json={"title": ""}).status_code == 422
    assert client.post("/api/notes", json={"title": "x" * 201}).status_code == 422


def test_user_b_cannot_see_or_delete_user_a_notes(client, other_client):
    sign_up_and_log_in(client, email="alice@example.com")
    sign_up_and_log_in(other_client, email="bob@example.com")
    note_id = client.post("/api/notes", json={"title": "Alice only"}).json()["id"]

    assert other_client.get("/api/notes").json() == []
    assert other_client.delete(f"/api/notes/{note_id}").status_code == 404  # 404, not 403 or 204
    assert [n["id"] for n in client.get("/api/notes").json()] == [note_id]  # still there


def test_timestamps_are_utc_with_offset(client):
    sign_up_and_log_in(client)
    created = client.post("/api/notes", json={"title": "t"}).json()["created_at"]
    assert created.endswith("Z") or created.endswith("+00:00")  # browsers parse this as UTC, not local time
    assert client.get("/api/auth/me").json()["created_at"].endswith(("Z", "+00:00"))
