from sqlalchemy import select

from app.core.errors import AppError
from app.core.security import password_hasher
from app.models import AuditEvent, Membership, Shipment, User, WebhookSource
from app.repositories.common import owned
from app.services.auth import require_role


def add_member(db, principal, email, role, password=None):
    require_role(principal, "ADMIN")
    if role not in {"ADMIN", "OPERATOR", "VIEWER"}:
        raise AppError(422, "invalid_role", "Choose ADMIN, OPERATOR, or VIEWER")
    user = db.scalar(select(User).where(User.email == email.lower()))
    if not user:
        if not password or not 12 <= len(password) <= 128:
            raise AppError(
                422, "invalid_password", "New members need a password of 12–128 characters"
            )
        user = User(email=email.lower(), password_hash=password_hasher.hash(password))
        db.add(user)
        db.flush()
    membership = db.scalar(
        select(Membership)
        .where(
            Membership.organization_id == principal.organization_id, Membership.user_id == user.id
        )
        .with_for_update()
    )
    if membership and membership.role == "ADMIN" and role != "ADMIN":
        admins = list(
            db.scalars(
                select(Membership)
                .where(
                    Membership.organization_id == principal.organization_id,
                    Membership.role == "ADMIN",
                )
                .with_for_update()
            )
        )
        if len(admins) <= 1:
            raise AppError(409, "last_admin", "Cannot remove the last organization admin")
    if membership:
        membership.role = role
    else:
        membership = Membership(
            organization_id=principal.organization_id, user_id=user.id, role=role
        )
        db.add(membership)
    db.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="membership.updated",
            resource_id=user.id,
            details={"role": role},
        )
    )
    db.commit()
    return membership


def create_shipment(db, principal, source_id, external_id):
    require_role(principal, "ADMIN")
    source = owned(db, WebhookSource, source_id, principal)
    if not external_id or len(external_id) > 100:
        raise AppError(422, "invalid_shipment_id", "Shipment ID must be 1–100 characters")
    existing = db.scalar(
        select(Shipment).where(
            Shipment.organization_id == principal.organization_id,
            Shipment.source_id == source.id,
            Shipment.external_id == external_id,
        )
    )
    if existing:
        return existing
    shipment = Shipment(
        organization_id=principal.organization_id, source_id=source.id, external_id=external_id
    )
    db.add(shipment)
    db.flush()
    db.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="shipment.created",
            resource_id=shipment.id,
            details={"external_id": external_id},
        )
    )
    db.commit()
    return shipment
