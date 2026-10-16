import pytest

from app.core.security import utcnow
from app.services.processing import process_job
from tests.conftest import headers
from tests.integration.test_processing import accept

pytestmark = pytest.mark.integration


def test_all_operator_pages_render(tenant, client, login_client, db, settings, body):
    _, source, secret, shipment = tenant()
    job = accept(db, source, secret, body(), settings, utcnow())
    process_job(db, job.id, settings)
    raw = body("missing", shipment_id="missing")
    response = client.post(
        f"/webhooks/{source.public_id}", content=raw, headers=headers(secret, raw)
    )
    event_id = response.json()["id"]
    login_client()
    for path in [
        "/",
        "/sources",
        "/sources/new",
        f"/sources/{source.id}",
        "/events",
        "/failed",
        f"/events/{event_id}",
        f"/events/{event_id}/replay",
        "/shipments",
        f"/shipments/{shipment.id}",
        "/notifications",
        "/audit",
    ]:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert "hookdesk" in response.text
    assert client.get("/events?state=QUEUED", headers={"HX-Request": "true"}).status_code == 200
    assert client.get("/", headers={"HX-Request": "true"}).status_code == 200


def test_operator_can_replay_viewer_cannot(tenant, client, login_client, db, settings, body):
    for role in ["OPERATOR", "VIEWER"]:
        principal, source, secret, _ = tenant(f"{role.lower()}@example.com", role)
        job = accept(db, source, secret, body(shipment_id="unknown"), settings, utcnow())
        assert process_job(db, job.id, settings) == "FAILED"
        from app.models import ProcessingRun

        event_id = db.get(ProcessingRun, job.run_id).event_id
        csrf = login_client(principal.email)
        response = client.post(
            f"/api/v1/events/{event_id}/replay",
            json={"reason": "Fixed shipment"},
            headers={**csrf, "Idempotency-Key": f"replay-{role}"},
        )
        assert response.status_code == (202 if role == "OPERATOR" else 403)
        assert (
            client.post(
                f"/api/v1/sources/{source.id}/rotate-secret", json={}, headers=csrf
            ).status_code
            == 403
        )


def test_all_resource_reads_are_tenant_scoped(tenant, client, login_client, db, settings, body):
    _, source, secret, shipment = tenant()
    raw = body()
    event_id = client.post(
        f"/webhooks/{source.public_id}", content=raw, headers=headers(secret, raw)
    ).json()["id"]
    tenant("other@example.com", "ADMIN", "Other")
    csrf = login_client("other@example.com")
    for suffix in ["", "/runs", "/attempts"]:
        assert client.get(f"/api/v1/events/{event_id}{suffix}").status_code == 404
    for suffix in ["", "/history"]:
        assert client.get(f"/api/v1/shipments/{shipment.id}{suffix}").status_code == 404
    assert (
        client.post(
            f"/api/v1/events/{event_id}/replay",
            json={"reason": "Attempt across tenant"},
            headers={**csrf, "Idempotency-Key": "cross-tenant"},
        ).status_code
        == 404
    )
    for path in ["events", "notifications"]:
        assert client.get(f"/api/v1/{path}").json()["total"] == 0


def test_payload_html_is_escaped(tenant, client, login_client, body):
    _, source, secret, _ = tenant()
    raw = body(event_id="<script>alert(1)</script>")
    event_id = client.post(
        f"/webhooks/{source.public_id}", content=raw, headers=headers(secret, raw)
    ).json()["id"]
    login_client()
    response = client.get(f"/events/{event_id}")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_invalid_filters_use_consistent_errors(tenant, client, login_client):
    tenant()
    login_client()
    for query in ["state=BOGUS", "page=0", "source_id=bad", "received_from=2026-01-01T00:00:00"]:
        response = client.get("/api/v1/events?" + query)
        assert response.status_code == 422, response.text
        assert set(response.json()) == {"code", "message", "request_id"}
