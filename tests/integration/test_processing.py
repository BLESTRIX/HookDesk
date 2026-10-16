from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.core.errors import AppError
from app.core.security import sign, utcnow
from app.models import (
    BusinessEffect,
    DemoFault,
    DurableJob,
    InternalNotification,
    ProcessingAttempt,
    ProcessingRun,
    Shipment,
    WebhookEvent,
)
from app.schemas import ReplayInput, SourcePatch
from app.services.ingestion import ingest
from app.services.processing import (
    SimulatedCrash,
    claim_job,
    due_jobs,
    execute_claim,
    process_job,
)
from app.services.replay import replay_event
from app.services.sources import update_source

pytestmark = pytest.mark.integration


def accept(db, source, secret, body, settings, now):
    ts = str(int(now.timestamp()))
    result = ingest(
        db, source.public_id, body, ts, sign(secret, ts, body), settings, "test", now=now
    )
    run = db.scalar(
        select(ProcessingRun)
        .where(ProcessingRun.event_id == result["id"])
        .order_by(ProcessingRun.number.desc())
    )
    return db.scalar(select(DurableJob).where(DurableJob.run_id == run.id))


def test_retry_exhaustion_replay_and_effect_deduplication(tenant, db, settings, body):
    principal, source, secret, shipment = tenant()
    now = utcnow()
    raw = body()
    job = accept(db, source, secret, raw, settings, now)
    db.add(
        DemoFault(
            organization_id=principal.organization_id,
            source_id=source.id,
            mode="timeout",
            remaining=100,
        )
    )
    db.commit()
    for attempt in range(1, settings.max_attempts + 1):
        outcome = process_job(db, job.id, settings, clock=lambda now=now: now)
        assert outcome == ("FAILED" if attempt == settings.max_attempts else "RETRY_WAIT")
        db.refresh(job)
        now = job.available_at + timedelta(seconds=1)
    original_run = db.get(ProcessingRun, job.run_id)
    event = db.get(WebhookEvent, original_run.event_id)
    assert original_run.attempt_count == 5 and event.state == "FAILED"
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == 0
    fault = db.scalar(select(DemoFault))
    fault.remaining = 0
    db.commit()
    data = ReplayInput(reason="Removed simulated dependency timeout")
    result = replay_event(db, principal, event.id, data, "replay-1")
    assert replay_event(db, principal, event.id, data, "replay-1") == result
    with pytest.raises(AppError) as exc:
        replay_event(db, principal, event.id, ReplayInput(reason="Different reason"), "replay-1")
    assert exc.value.code == "idempotency_conflict"
    db.rollback()
    replay_job = db.scalar(select(DurableJob).where(DurableJob.run_id == result["run_id"]))
    now = max(now, utcnow())
    assert process_job(db, replay_job.id, settings, clock=lambda: now) == "SUCCEEDED"
    for _ in range(5):
        ts = str(int(now.timestamp()))
        assert ingest(
            db, source.public_id, raw, ts, sign(secret, ts, raw), settings, "test", now=now
        )["duplicate"]
    assert db.scalar(select(func.count()).select_from(ProcessingAttempt)) == 6
    assert db.scalar(select(func.count()).select_from(ProcessingRun)) == 2
    assert db.scalar(select(func.count()).select_from(DurableJob)) == 2
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == 1
    assert db.scalar(select(func.count()).select_from(InternalNotification)) == 1
    db.refresh(shipment)
    assert shipment.status == "shipped" and shipment.last_sequence == 1
    with pytest.raises(AppError, match="Only FAILED"):
        replay_event(db, principal, event.id, data, "another-key")


def test_abandoned_recovery_and_fencing(tenant, factory, db, settings, body):
    _, source, secret, _ = tenant()
    now = utcnow()
    job = accept(db, source, secret, body(), settings, now)
    with factory() as first, factory() as second:
        stale = claim_job(first, job.id, settings, clock=lambda: now)
        later = now + timedelta(seconds=settings.lease_seconds + 1)
        assert job.id in due_jobs(second, now=later)
        new = claim_job(second, job.id, settings, clock=lambda: later)
        assert new.owner_token != stale.owner_token
        assert execute_claim(first, stale, settings, clock=lambda: later) == "LOST_LEASE"
        assert execute_claim(second, new, settings, clock=lambda: later) == "SUCCEEDED"
    outcomes = list(
        db.scalars(select(ProcessingAttempt.outcome).order_by(ProcessingAttempt.number))
    )
    assert outcomes == ["ABANDONED", "SUCCEEDED"]
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == 1


def test_expiry_during_handler_rolls_back_all_effects(tenant, db, settings, body, monkeypatch):
    from app.services import processing

    _, source, secret, shipment = tenant()
    now = utcnow()
    job = accept(db, source, secret, body(), settings, now)
    claim = claim_job(db, job.id, settings, clock=lambda: now)
    actual_handler = processing.apply_shipment_event
    clock_value = [now]

    def slow_handler(*args):
        result = actual_handler(*args)
        clock_value[0] += timedelta(seconds=settings.lease_seconds + 1)
        return result

    monkeypatch.setattr(processing, "apply_shipment_event", slow_handler)
    assert execute_claim(db, claim, settings, clock=lambda: clock_value[0]) == "LOST_LEASE"
    db.refresh(shipment)
    assert shipment.status == "pending"
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == 0
    assert db.scalar(select(func.count()).select_from(InternalNotification)) == 0


@pytest.mark.parametrize(
    "sequence,status,expected,code",
    [
        (1, "shipped", "IGNORED", None),
        (2, "shipped", "IGNORED", None),
        (2, "delivered", "FAILED", "sequence_conflict"),
        (3, "shipped", "SUCCEEDED", None),
        (3, "delivered", "SUCCEEDED", None),
    ],
)
def test_sequence_rules(tenant, db, settings, body, sequence, status, expected, code):
    _, source, secret, shipment = tenant()
    shipment.status, shipment.last_sequence = "shipped", 2
    db.commit()
    now = utcnow()
    job = accept(db, source, secret, body(sequence=sequence, status=status), settings, now)
    assert process_job(db, job.id, settings, clock=lambda: now) == expected
    run = db.get(ProcessingRun, job.run_id)
    assert run.error_code == code
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == (
        1 if status == "delivered" and sequence == 3 else 0
    )


def test_pending_to_delivered_and_late_shipped(tenant, db, settings, body):
    _, source, secret, shipment = tenant()
    now = utcnow()
    latest = accept(db, source, secret, body("latest", 2, "delivered"), settings, now)
    assert process_job(db, latest.id, settings, clock=lambda: now) == "SUCCEEDED"
    old = accept(db, source, secret, body("older", 1, "shipped"), settings, now)
    assert process_job(db, old.id, settings, clock=lambda: now) == "IGNORED"
    regression = accept(db, source, secret, body("regression", 3, "shipped"), settings, now)
    assert process_job(db, regression.id, settings, clock=lambda: now) == "FAILED"
    assert db.get(ProcessingRun, regression.run_id).error_code == "state_regression"
    db.refresh(shipment)
    assert shipment.status == "delivered"


def test_unknown_shipment_fails_immediately_then_replay(tenant, db, settings, body):
    principal, source, secret, _ = tenant()
    now = utcnow()
    job = accept(db, source, secret, body(shipment_id="missing"), settings, now)
    assert process_job(db, job.id, settings, clock=lambda: now) == "FAILED"
    run = db.get(ProcessingRun, job.run_id)
    assert run.attempt_count == 1 and run.error_code == "unknown_shipment"
    db.add(
        Shipment(
            organization_id=principal.organization_id, source_id=source.id, external_id="missing"
        )
    )
    db.commit()
    result = replay_event(
        db, principal, run.event_id, ReplayInput(reason="Created missing shipment"), "new-shipment"
    )
    new_job = db.scalar(select(DurableJob).where(DurableJob.run_id == result["run_id"]))
    assert process_job(db, new_job.id, settings) == "SUCCEEDED"


def test_deactivated_source_does_not_cancel_accepted_work(tenant, db, settings, body):
    principal, source, secret, _ = tenant()
    now = utcnow()
    job = accept(db, source, secret, body(), settings, now)
    update_source(db, principal, source.id, SourcePatch(active=False))
    assert process_job(db, job.id, settings, clock=lambda: now) == "SUCCEEDED"


def test_crash_fault_is_recovered(tenant, db, settings, body):
    principal, source, secret, _ = tenant()
    now = utcnow()
    job = accept(db, source, secret, body(), settings, now)
    db.add(
        DemoFault(
            organization_id=principal.organization_id,
            source_id=source.id,
            mode="crash",
            remaining=1,
        )
    )
    db.commit()
    with pytest.raises(SimulatedCrash):
        process_job(db, job.id, settings, clock=lambda: now)
    now += timedelta(seconds=settings.lease_seconds + 1)
    assert process_job(db, job.id, settings, clock=lambda: now) == "SUCCEEDED"


def test_envelope_is_immutable_in_postgres(tenant, db, settings, body):
    _, source, secret, _ = tenant()
    job = accept(db, source, secret, body(), settings, utcnow())
    run = db.get(ProcessingRun, job.run_id)
    event = db.get(WebhookEvent, run.event_id)
    event.raw_body = b"edited"
    with pytest.raises(DBAPIError):
        db.commit()
    db.rollback()
