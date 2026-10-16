"""Database entities. Composite foreign keys enforce tenant consistency."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import utcnow
from app.db.base import Base, new_id

STATES = "'QUEUED','PROCESSING','RETRY_WAIT','SUCCEEDED','IGNORED','FAILED'"


class Identity:
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)


class Owned(Identity):
    organization_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("organizations.id"), index=True
    )


class Organization(Identity, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Identity, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Membership(Owned, Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(16))
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        CheckConstraint("role IN ('ADMIN','OPERATOR','VIEWER')", name="role"),
    )


class Session(Owned, Base):
    __tablename__ = "sessions"
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"], ["memberships.organization_id", "memberships.user_id"]
        ),
    )


class LoginBucket(Base):
    __tablename__ = "login_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebhookSource(Owned, Base):
    __tablename__ = "webhook_sources"
    name: Mapped[str] = mapped_column(String(120))
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("id", "organization_id"),)


class EncryptedSigningSecret(Owned, Base):
    __tablename__ = "encrypted_signing_secrets"
    source_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "organization_id"],
            ["webhook_sources.id", "webhook_sources.organization_id"],
        ),
        Index(
            "uq_source_current_secret",
            "source_id",
            unique=True,
            postgresql_where=text("expires_at IS NULL"),
        ),
    )


class WebhookEvent(Owned, Base):
    __tablename__ = "webhook_events"
    source_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    external_event_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(80))
    shipment_external_id: Mapped[str] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_body: Mapped[bytes] = mapped_column(LargeBinary)
    payload: Mapped[dict] = mapped_column(JSONB)
    body_hash: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="QUEUED")
    latest_run_number: Mapped[int] = mapped_column(Integer, default=1)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint("source_id", "external_event_id"),
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["source_id", "organization_id"],
            ["webhook_sources.id", "webhook_sources.organization_id"],
        ),
        CheckConstraint(f"state IN ({STATES})", name="state"),
        Index("ix_event_org_received", "organization_id", "received_at", "id"),
        Index("ix_event_org_state_received", "organization_id", "state", "received_at"),
        Index("ix_event_org_shipment", "organization_id", "shipment_external_id"),
    )


class ProcessingRun(Owned, Base):
    __tablename__ = "processing_runs"
    event_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="QUEUED")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_by: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id"))
    replay_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_summary: Mapped[str | None] = mapped_column(String(300))
    __table_args__ = (
        UniqueConstraint("event_id", "number"),
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["event_id", "organization_id"], ["webhook_events.id", "webhook_events.organization_id"]
        ),
        CheckConstraint(f"state IN ({STATES})", name="state"),
    )


class DurableJob(Owned, Base):
    __tablename__ = "durable_jobs"
    run_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), unique=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_token: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["run_id", "organization_id"], ["processing_runs.id", "processing_runs.organization_id"]
        ),
        Index("ix_job_due", "available_at", postgresql_where=text("NOT completed")),
        Index("ix_job_lease", "lease_until", postgresql_where=text("NOT completed")),
    )


class ProcessingAttempt(Owned, Base):
    __tablename__ = "processing_attempts"
    run_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    number: Mapped[int] = mapped_column(Integer)
    owner_token: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(20), default="PROCESSING")
    handler_version: Mapped[str] = mapped_column(String(40), default="shipment-v1")
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_summary: Mapped[str | None] = mapped_column(String(300))
    __table_args__ = (
        UniqueConstraint("run_id", "number"),
        ForeignKeyConstraint(
            ["run_id", "organization_id"], ["processing_runs.id", "processing_runs.organization_id"]
        ),
    )


class Shipment(Owned, Base):
    __tablename__ = "shipments"
    source_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    external_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    last_sequence: Mapped[int] = mapped_column(Numeric(), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["source_id", "organization_id"],
            ["webhook_sources.id", "webhook_sources.organization_id"],
        ),
        CheckConstraint("status IN ('pending','shipped','delivered')", name="status"),
        CheckConstraint("last_sequence >= 0", name="sequence"),
    )


class BusinessEffect(Owned, Base):
    __tablename__ = "business_effects"
    shipment_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    event_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    effect_key: Mapped[str] = mapped_column(String(200))
    previous_status: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    sequence: Mapped[int] = mapped_column(Numeric())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("organization_id", "effect_key"),
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["shipment_id", "organization_id"], ["shipments.id", "shipments.organization_id"]
        ),
        ForeignKeyConstraint(
            ["event_id", "organization_id"], ["webhook_events.id", "webhook_events.organization_id"]
        ),
    )


class InternalNotification(Owned, Base):
    __tablename__ = "internal_notifications"
    effect_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), unique=True)
    message: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["effect_id", "organization_id"],
            ["business_effects.id", "business_effects.organization_id"],
        ),
        Index("ix_notification_org_time", "organization_id", "created_at"),
    )


class IdempotencyRecord(Owned, Base):
    __tablename__ = "idempotency_records"
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id"))
    operation: Mapped[str] = mapped_column(String(80))
    key_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("organization_id", "user_id", "operation", "key_hash"),)


class AuditEvent(Owned, Base):
    __tablename__ = "audit_events"
    actor_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_audit_org_time", "organization_id", "created_at"),)


class DemoFault(Owned, Base):
    __tablename__ = "demo_faults"
    source_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    mode: Mapped[str] = mapped_column(String(20))
    remaining: Mapped[int] = mapped_column(Integer, default=100)
    __table_args__ = (
        UniqueConstraint("source_id"),
        ForeignKeyConstraint(
            ["source_id", "organization_id"],
            ["webhook_sources.id", "webhook_sources.organization_id"],
        ),
        CheckConstraint("mode IN ('timeout','crash')", name="mode"),
    )


class OperationalCounter(Base):
    __tablename__ = "operational_counters"
    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReadinessProbe(Base):
    __tablename__ = "readiness_probes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
