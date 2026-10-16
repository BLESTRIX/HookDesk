import json

from sqlalchemy import select, text

from app.core.errors import AppError
from app.core.security import digest
from app.models import AuditEvent, DurableJob, IdempotencyRecord, ProcessingRun, WebhookEvent
from app.repositories.common import owned
from app.services.auth import require_role


def replay_event(db, principal, event_id, data, idempotency_key):
    require_role(principal, "ADMIN", "OPERATOR")
    if not idempotency_key or not 1 <= len(idempotency_key) <= 200:
        raise AppError(
            422, "idempotency_key_required", "Provide an Idempotency-Key of 1–200 characters"
        )
    key_hash = digest(idempotency_key)
    fingerprint = digest(json.dumps({"event_id": event_id, "reason": data.reason}, sort_keys=True))
    scope = f"{principal.organization_id}:{principal.user_id}:replay:{key_hash}"
    # Serialize identical keys even when the caller targets different event rows.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": scope})
    previous = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.organization_id == principal.organization_id,
            IdempotencyRecord.user_id == principal.user_id,
            IdempotencyRecord.operation == "replay",
            IdempotencyRecord.key_hash == key_hash,
        )
    )
    if previous:
        if previous.fingerprint != fingerprint:
            raise AppError(
                409, "idempotency_conflict", "This key was used with different replay input"
            )
        response = previous.response
        db.commit()
        return response
    event = owned(db, WebhookEvent, event_id, principal, lock=True)
    if event.state != "FAILED":
        raise AppError(
            409, "replay_not_eligible", "Only FAILED events without active work can be replayed"
        )
    event.latest_run_number += 1
    event.state = "QUEUED"
    run = ProcessingRun(
        organization_id=principal.organization_id,
        event_id=event.id,
        number=event.latest_run_number,
        requested_by=principal.user_id,
        replay_reason=data.reason,
    )
    db.add(run)
    db.flush()
    db.add(DurableJob(organization_id=principal.organization_id, run_id=run.id))
    response = {"event_id": event.id, "run_id": run.id, "run_number": run.number, "state": "QUEUED"}
    db.add(
        IdempotencyRecord(
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            operation="replay",
            key_hash=key_hash,
            fingerprint=fingerprint,
            response=response,
        )
    )
    db.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="event.replayed",
            resource_id=event.id,
            details={"reason": data.reason, "run_id": run.id},
        )
    )
    db.commit()
    return response
