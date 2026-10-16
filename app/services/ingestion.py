import json
import logging

from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from app.core.errors import AppError
from app.core.security import decrypt_secret, digest, utcnow, verify_signature
from app.db.base import new_id
from app.models import (
    DurableJob,
    EncryptedSigningSecret,
    OperationalCounter,
    ProcessingRun,
    WebhookEvent,
    WebhookSource,
)
from app.schemas import CourierEvent

logger = logging.getLogger(__name__)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def parse_event(body: bytes) -> CourierEvent:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
        return CourierEvent.model_validate(payload)
    except ValidationError as exc:
        # Do not echo arbitrary request values or the original body in errors.
        fields = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in exc.errors(include_input=False, include_context=False)
        )
        raise AppError(422, "invalid_payload", fields[:1000]) from None
    except (ValueError, UnicodeError, RecursionError):
        raise AppError(422, "invalid_json", "Use valid UTF-8 JSON without duplicate keys") from None


def increment_counter(db, code):
    db.execute(
        insert(OperationalCounter)
        .values(code=code, count=1)
        .on_conflict_do_update(
            index_elements=[OperationalCounter.code], set_={"count": OperationalCounter.count + 1}
        )
    )


def ingest(db, public_source_id, body, timestamp, signature, settings, correlation_id, *, now=None):
    now = now or utcnow()
    if len(body) > settings.max_body_bytes:
        raise AppError(413, "body_too_large", "Maximum webhook body is 256 KiB by default")
    # A shared lock makes deactivation and secret rotation linearizable with acceptance.
    source = db.scalar(
        select(WebhookSource)
        .where(WebhookSource.public_id == public_source_id)
        .with_for_update(read=True)
    )
    if not source or not source.active:
        raise AppError(404, "source_unavailable", "Webhook source is unavailable")
    encrypted = list(
        db.scalars(
            select(EncryptedSigningSecret).where(
                EncryptedSigningSecret.source_id == source.id,
                or_(
                    EncryptedSigningSecret.expires_at.is_(None),
                    EncryptedSigningSecret.expires_at > now,
                ),
            )
        )
    )
    try:
        secrets_ = [decrypt_secret(s.ciphertext, settings.encryption_key) for s in encrypted]
    except InvalidToken:
        raise AppError(
            503,
            "signing_configuration_unavailable",
            "Signing configuration is unavailable; retry delivery later",
        ) from None
    verify_signature(
        secrets_,
        timestamp,
        signature,
        body,
        now=now,
        tolerance=settings.signature_tolerance_seconds,
    )
    event = parse_event(body)
    event_id = new_id()
    body_hash = digest(body)
    inserted = db.scalar(
        insert(WebhookEvent)
        .values(
            id=event_id,
            organization_id=source.organization_id,
            source_id=source.id,
            external_event_id=event.event_id,
            event_type=event.event_type,
            shipment_external_id=event.data.shipment_id,
            occurred_at=event.occurred_at,
            received_at=now,
            raw_body=body,
            payload=event.model_dump(mode="json"),
            body_hash=body_hash,
            correlation_id=correlation_id,
            state="QUEUED",
            latest_run_number=1,
            duplicate_count=0,
        )
        .on_conflict_do_nothing(
            index_elements=[WebhookEvent.source_id, WebhookEvent.external_event_id]
        )
        .returning(WebhookEvent.id)
    )
    if inserted is None:
        previous = db.scalar(
            select(WebhookEvent)
            .where(
                WebhookEvent.source_id == source.id,
                WebhookEvent.external_event_id == event.event_id,
            )
            .with_for_update()
        )
        if previous.body_hash != body_hash:
            raise AppError(
                409, "event_id_conflict", "This event ID was accepted with different body bytes"
            )
        previous.duplicate_count += 1
        increment_counter(db, "duplicate_delivery")
        db.commit()
        return {"id": previous.id, "duplicate": True}
    run = ProcessingRun(organization_id=source.organization_id, event_id=event_id, number=1)
    db.add(run)
    db.flush()
    job = DurableJob(organization_id=source.organization_id, run_id=run.id, available_at=now)
    db.add(job)
    db.commit()  # No broker call: acknowledgment follows the durable PostgreSQL commit.
    logger.info(
        "event.accepted",
        extra={
            "event_id": event_id,
            "run_id": run.id,
            "job_id": job.id,
            "correlation_id": correlation_id,
        },
    )
    return {"id": event_id, "duplicate": False}
