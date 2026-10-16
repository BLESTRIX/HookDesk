import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import utcnow
from app.models import BusinessEffect, InternalNotification, ProcessingRun, WebhookEvent
from app.schemas import ReplayInput
from app.services.auth import rate_limit
from app.services.processing import claim_job, process_job
from app.services.replay import replay_event
from tests.conftest import headers
from tests.integration.test_processing import accept

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "field,value",
    [
        ("occurred_at", 1000),
        ("occurred_at", "2026-10-16T03:00:00"),
        ("event_id", "contains\x00null"),
        ("event_id", "\ud800"),
        ("sequence", True),
        ("sequence", "1"),
        ("sequence", 0),
        ("sequence", -1),
    ],
)
def test_invalid_contract_values_are_not_persisted(tenant, client, db, body, field, value):
    _, source, secret, _ = tenant()
    payload = json.loads(body())
    if field == "sequence":
        payload["data"][field] = value
    else:
        payload[field] = value
    raw = json.dumps(payload).encode()
    response = client.post(
        f"/webhooks/{source.public_id}", content=raw, headers=headers(secret, raw)
    )
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_sequence_is_not_limited_by_machine_integer(tenant, db, body, settings):
    _, source, secret, shipment = tenant()
    sequence = 10**50 + 1
    job = accept(db, source, secret, body(sequence=sequence), settings, utcnow())
    assert process_job(db, job.id, settings) == "SUCCEEDED"
    db.refresh(shipment)
    assert shipment.last_sequence == sequence


def test_abandoned_final_attempt_exhausts_budget(tenant, db, body, settings):
    _, source, secret, _ = tenant()
    now = utcnow()
    job = accept(db, source, secret, body(), settings, now)
    limited = settings.model_copy(update={"max_attempts": 1})
    assert claim_job(db, job.id, limited, clock=lambda: now)
    later = now + timedelta(seconds=settings.lease_seconds + 1)
    assert claim_job(db, job.id, limited, clock=lambda: later) is None
    run = db.get(ProcessingRun, job.run_id)
    assert run.state == "FAILED" and run.error_code == "attempts_exhausted"
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == 0


def test_login_rate_limit_persists(tenant, db, settings):
    tenant()
    limited = settings.model_copy(update={"login_limit": 1})
    rate_limit(db, "rate-limited-test", limited)
    with pytest.raises(AppError) as exc:
        rate_limit(db, "rate-limited-test", limited)
    assert exc.value.status == 429


def test_replay_key_is_scoped_per_user_and_fingerprints_event(tenant, db, settings, body):
    principal, source, secret, _ = tenant()
    now = utcnow()
    jobs = [
        accept(db, source, secret, body(f"missing-{i}", shipment_id="unknown"), settings, now)
        for i in range(2)
    ]
    for job in jobs:
        assert process_job(db, job.id, settings) == "FAILED"
    first, second = [db.get(ProcessingRun, job.run_id).event_id for job in jobs]
    reason = ReplayInput(reason="Created the shipment")
    replay_event(db, principal, first, reason, "same-key")
    with pytest.raises(AppError) as exc:
        replay_event(db, principal, second, reason, "same-key")
    assert exc.value.code == "idempotency_conflict"
    db.rollback()
    # A separate organization/user can independently use the same key.
    other, other_source, other_secret, _ = tenant("other@example.com")
    job = accept(db, other_source, other_secret, body(shipment_id="unknown"), settings, utcnow())
    assert process_job(db, job.id, settings) == "FAILED"
    assert (
        replay_event(db, other, db.get(ProcessingRun, job.run_id).event_id, reason, "same-key")[
            "state"
        ]
        == "QUEUED"
    )


def test_database_failure_rolls_back_partial_handler_effects(
    tenant, db, settings, body, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from app.services import processing

    _, source, secret, shipment = tenant()
    job = accept(db, source, secret, body(), settings, utcnow())
    actual = processing.apply_shipment_event

    def failing_handler(*args):
        actual(*args)
        raise OperationalError("synthetic transient failure", None, Exception("test"))

    monkeypatch.setattr(processing, "apply_shipment_event", failing_handler)
    assert process_job(db, job.id, settings) == "RETRY_WAIT"
    db.refresh(shipment)
    assert shipment.status == "pending"
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == 0
    assert db.scalar(select(func.count()).select_from(InternalNotification)) == 0


def test_prod_rejects_demo_and_insecure_cookies(settings):
    for secure, demo in [(False, False), (True, True)]:
        with pytest.raises(ValidationError):
            Settings(
                encryption_key=settings.encryption_key,
                environment="production",
                cookie_secure=secure,
                demo_enabled=demo,
            )


def test_docs_and_vendor_assets_are_local(client):
    response = client.get("/docs")
    assert (
        response.status_code == 200 and 'src="/static/vendor/swagger-ui-bundle.js"' in response.text
    )
    schema = client.get("/openapi.json").json()
    assert "CourierEvent" in schema["components"]["schemas"]
    assert "/login" not in schema["paths"]
    assert schema["paths"]["/api/v1/events"]["get"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("ErrorResponse")
