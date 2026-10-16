from sqlalchemy import select

from app.core.errors import PermanentFailure
from app.db.base import new_id
from app.models import BusinessEffect, InternalNotification, Shipment

HANDLER_VERSION = "shipment-v1"
RANK = {"pending": 0, "shipped": 1, "delivered": 2}


def apply_shipment_event(db, event, now):
    data = event.payload["data"]
    shipment = db.scalar(
        select(Shipment)
        .where(
            Shipment.organization_id == event.organization_id,
            Shipment.source_id == event.source_id,
            Shipment.external_id == data["shipment_id"],
        )
        .with_for_update()
    )
    if not shipment:
        raise PermanentFailure(
            "unknown_shipment", "Create this shipment before replaying the event"
        )
    sequence, status = data["sequence"], data["status"]
    if sequence < shipment.last_sequence:
        return "IGNORED"
    if sequence == shipment.last_sequence:
        if status != shipment.status:
            raise PermanentFailure(
                "sequence_conflict", "Equal sequence has a different shipment status"
            )
        return "IGNORED"
    if RANK[status] < RANK[shipment.status]:
        raise PermanentFailure("state_regression", "This event would regress shipment status")
    if status != shipment.status:
        effect = BusinessEffect(
            id=new_id(),
            organization_id=event.organization_id,
            shipment_id=shipment.id,
            event_id=event.id,
            effect_key=f"shipment:{shipment.id}:status:{status}",
            previous_status=shipment.status,
            status=status,
            sequence=sequence,
            created_at=now,
        )
        db.add(effect)
        db.flush()
        db.add(
            InternalNotification(
                organization_id=event.organization_id,
                effect_id=effect.id,
                message=f"{shipment.external_id} is now {status}",
                created_at=now,
            )
        )
    shipment.status, shipment.last_sequence, shipment.updated_at = status, sequence, now
    return "SUCCEEDED"
