from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.inspection import inspect

from app.core.security import utcnow
from app.models import (
    AuditEvent,
    BusinessEffect,
    InternalNotification,
    ProcessingAttempt,
    ProcessingRun,
    Shipment,
    WebhookEvent,
    WebhookSource,
)
from app.repositories.common import owned, paginate

HIDDEN = {"raw_body", "ciphertext", "password_hash", "token_hash", "csrf_hash", "owner_token"}


def serialize(row, *, payload=True):
    return {
        c.key: getattr(row, c.key)
        for c in inspect(type(row)).columns
        if c.key not in HIDDEN and (payload or c.key != "payload")
    }


def list_page(db, query, page=1, page_size=25, *, payload=True):
    rows, total = paginate(db, query, page, page_size)
    return {
        "items": [serialize(r, payload=payload) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def sources(db, principal, page=1, page_size=25):
    return list_page(
        db,
        select(WebhookSource)
        .where(WebhookSource.organization_id == principal.organization_id)
        .order_by(WebhookSource.created_at.desc()),
        page,
        page_size,
    )


def events(db, principal, filters):
    query = select(WebhookEvent).where(WebhookEvent.organization_id == principal.organization_id)
    for field, column in [
        ("source_id", WebhookEvent.source_id),
        ("state", WebhookEvent.state),
        ("event_type", WebhookEvent.event_type),
        ("external_event_id", WebhookEvent.external_event_id),
        ("shipment_id", WebhookEvent.shipment_external_id),
    ]:
        value = getattr(filters, field)
        if value:
            query = query.where(column == value)
    if filters.received_from:
        query = query.where(WebhookEvent.received_at >= filters.received_from)
    if filters.received_to:
        query = query.where(WebhookEvent.received_at <= filters.received_to)
    return list_page(
        db,
        query.order_by(WebhookEvent.received_at.desc(), WebhookEvent.id),
        filters.page,
        filters.page_size,
        payload=False,
    )


def event_runs(db, principal, event_id, page=1, page_size=25):
    event = owned(db, WebhookEvent, event_id, principal)
    return list_page(
        db,
        select(ProcessingRun)
        .where(
            ProcessingRun.organization_id == principal.organization_id,
            ProcessingRun.event_id == event.id,
        )
        .order_by(ProcessingRun.number.desc()),
        page,
        page_size,
    )


def event_attempts(db, principal, event_id, page=1, page_size=25):
    event = owned(db, WebhookEvent, event_id, principal)
    query = (
        select(ProcessingAttempt)
        .join(ProcessingRun, ProcessingRun.id == ProcessingAttempt.run_id)
        .where(
            ProcessingAttempt.organization_id == principal.organization_id,
            ProcessingRun.organization_id == principal.organization_id,
            ProcessingRun.event_id == event.id,
        )
        .order_by(ProcessingRun.number.desc(), ProcessingAttempt.number.desc())
    )
    return list_page(db, query, page, page_size)


def shipments(db, principal, page=1, page_size=25):
    return list_page(
        db,
        select(Shipment)
        .where(Shipment.organization_id == principal.organization_id)
        .order_by(Shipment.updated_at.desc(), Shipment.id),
        page,
        page_size,
    )


def shipment_history(db, principal, shipment_id, page=1, page_size=25):
    shipment = owned(db, Shipment, shipment_id, principal)
    return list_page(
        db,
        select(BusinessEffect)
        .where(
            BusinessEffect.organization_id == principal.organization_id,
            BusinessEffect.shipment_id == shipment.id,
        )
        .order_by(BusinessEffect.created_at.desc()),
        page,
        page_size,
    )


def notifications(db, principal, page=1, page_size=25):
    return list_page(
        db,
        select(InternalNotification)
        .where(InternalNotification.organization_id == principal.organization_id)
        .order_by(InternalNotification.created_at.desc()),
        page,
        page_size,
    )


def audit_events(db, principal, page=1, page_size=25):
    return list_page(
        db,
        select(AuditEvent)
        .where(AuditEvent.organization_id == principal.organization_id)
        .order_by(AuditEvent.created_at.desc()),
        page,
        page_size,
    )


def dashboard(db, principal, *, now=None):
    now = now or utcnow()
    org = principal.organization_id
    since = now - timedelta(hours=24)
    totals = dict(
        db.execute(
            select(WebhookEvent.state, func.count())
            .where(WebhookEvent.organization_id == org)
            .group_by(WebhookEvent.state)
        ).all()
    )
    received = db.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.organization_id == org, WebhookEvent.received_at >= since)
    )
    duplicates = db.scalar(
        select(func.coalesce(func.sum(WebhookEvent.duplicate_count), 0)).where(
            WebhookEvent.organization_id == org
        )
    )
    median = db.scalar(
        select(func.percentile_cont(0.5).within_group(ProcessingAttempt.duration_ms)).where(
            ProcessingAttempt.organization_id == org,
            ProcessingAttempt.completed_at >= since,
            ProcessingAttempt.outcome != "ABANDONED",
        )
    )
    oldest = db.scalar(
        select(func.min(ProcessingRun.created_at)).where(
            ProcessingRun.organization_id == org, ProcessingRun.state == "QUEUED"
        )
    )
    return {
        "received_24h": received,
        "duplicate_deliveries_all_time": duplicates,
        "queue_depth": totals.get("QUEUED", 0),
        "processing": totals.get("PROCESSING", 0),
        "retry_wait": totals.get("RETRY_WAIT", 0),
        "failed": totals.get("FAILED", 0),
        "succeeded": totals.get("SUCCEEDED", 0),
        "ignored": totals.get("IGNORED", 0),
        "median_processing_ms_24h": round(median, 2) if median is not None else None,
        "oldest_queued_age_seconds": max(0, round((now - oldest).total_seconds()))
        if oldest
        else None,
        "window_start": since,
        "window_end": now,
    }
