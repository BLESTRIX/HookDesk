from datetime import timedelta

from sqlalchemy import delete, select

from app.core.errors import AppError
from app.core.security import encrypt_secret, new_token, utcnow
from app.models import AuditEvent, EncryptedSigningSecret, WebhookSource
from app.repositories.common import owned
from app.services.auth import require_role


def create_source(db, principal, data, settings):
    require_role(principal, "ADMIN")
    source = WebhookSource(
        organization_id=principal.organization_id, name=data.name, public_id=new_token()
    )
    db.add(source)
    db.flush()
    secret = new_token()
    db.add(
        EncryptedSigningSecret(
            organization_id=principal.organization_id,
            source_id=source.id,
            ciphertext=encrypt_secret(secret, settings.encryption_key),
        )
    )
    db.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="source.created",
            resource_id=source.id,
            details={},
        )
    )
    db.commit()
    return source, secret


def update_source(db, principal, source_id, data):
    require_role(principal, "ADMIN")
    source = owned(db, WebhookSource, source_id, principal, lock=True)
    changes = data.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise AppError(422, "invalid_source", "Source fields cannot be null")
    for key, value in changes.items():
        setattr(source, key, value)
    source.updated_at = utcnow()
    db.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="source.updated",
            resource_id=source.id,
            details=changes,
        )
    )
    db.commit()
    return source


def rotate_secret(db, principal, source_id, grace_seconds, settings):
    require_role(principal, "ADMIN")
    source = owned(db, WebhookSource, source_id, principal, lock=True)
    now = utcnow()
    # Keep at most the current and immediately previous secret.
    db.execute(
        delete(EncryptedSigningSecret).where(
            EncryptedSigningSecret.source_id == source.id,
            EncryptedSigningSecret.organization_id == principal.organization_id,
            EncryptedSigningSecret.expires_at.is_not(None),
        )
    )
    current = db.scalar(
        select(EncryptedSigningSecret)
        .where(
            EncryptedSigningSecret.source_id == source.id,
            EncryptedSigningSecret.organization_id == principal.organization_id,
            EncryptedSigningSecret.expires_at.is_(None),
        )
        .with_for_update()
    )
    grace = settings.secret_grace_seconds if grace_seconds is None else grace_seconds
    current.expires_at = now + timedelta(seconds=grace)
    db.flush()  # Release the partial unique-index slot before inserting the new current secret.
    secret = new_token()
    db.add(
        EncryptedSigningSecret(
            organization_id=principal.organization_id,
            source_id=source.id,
            ciphertext=encrypt_secret(secret, settings.encryption_key),
        )
    )
    source.updated_at = now
    db.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action="source.secret_rotated",
            resource_id=source.id,
            details={"grace_seconds": grace},
        )
    )
    db.commit()
    return source, secret
