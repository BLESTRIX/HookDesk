import pytest
from sqlalchemy import select

from app.models import Session

pytestmark = pytest.mark.integration


def test_register_logout_and_hashed_sessions(client, db):
    assert client.post("/api/v1/auth/register", json={}).status_code == 422
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "new@example.com",
            "password": "SomeLongPassword123!",
            "organization_name": "My Shop",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    stored = db.scalar(select(Session))
    assert stored.token_hash != client.cookies.get("hookdesk_session")
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert client.get("/api/v1/auth/me").json()["role"] == "ADMIN"
    token = client.cookies.get("hookdesk_session")
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert (
        client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": response.json()["csrf_token"]}
        ).status_code
        == 200
    )
    client.cookies.set("hookdesk_session", token)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_tenant_isolation_and_source_permissions(tenant, client, login_client):
    _, source, _, _ = tenant()
    tenant("other@example.com", "VIEWER", "Other shop")
    csrf = login_client("other@example.com")
    assert client.get(f"/api/v1/sources/{source.id}").status_code == 404
    assert client.get("/api/v1/sources").json()["total"] == 1
    assert (
        client.post("/api/v1/sources", json={"name": "Forbidden"}, headers=csrf).status_code == 403
    )


def test_one_time_secret_and_csrf(tenant, client, login_client):
    tenant()
    csrf = login_client()
    assert client.post("/api/v1/sources", json={"name": "New"}).status_code == 403
    response = client.post("/api/v1/sources", json={"name": "New"}, headers=csrf)
    assert response.status_code == 201
    data = response.json()
    assert data["signing_secret"]
    detail = client.get(f"/api/v1/sources/{data['id']}").json()
    assert "signing_secret" not in detail and "ciphertext" not in detail
    assert (
        client.patch(
            f"/api/v1/sources/{data['id']}", json={"active": False}, headers=csrf
        ).status_code
        == 200
    )


def test_login_requires_preauth_csrf(client):
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "a@example.com", "password": "x"}
        ).status_code
        == 403
    )
