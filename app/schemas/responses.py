"""Explicit public response contracts; no secrets or worker ownership tokens in read models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

State = Literal["QUEUED", "PROCESSING", "RETRY_WAIT", "SUCCEEDED", "IGNORED", "FAILED"]


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


class SourceOut(BaseModel):
    id: str
    organization_id: str
    name: str
    public_id: str
    active: bool
    created_at: datetime
    updated_at: datetime


class SourceCreated(SourceOut):
    signing_secret: str


class EventOut(BaseModel):
    id: str
    organization_id: str
    source_id: str
    external_event_id: str
    event_type: Literal["shipment.status_changed"]
    shipment_external_id: str
    occurred_at: datetime
    received_at: datetime
    body_hash: str
    correlation_id: str
    state: State
    latest_run_number: int
    duplicate_count: int


class EventDetail(EventOut):
    payload: dict


class RunOut(BaseModel):
    id: str
    organization_id: str
    event_id: str
    number: int
    state: State
    attempt_count: int
    requested_by: str | None
    replay_reason: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    next_retry_at: datetime | None
    error_code: str | None
    error_summary: str | None


class AttemptOut(BaseModel):
    id: str
    organization_id: str
    run_id: str
    number: int
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    outcome: str
    handler_version: str
    error_code: str | None
    error_summary: str | None


class ShipmentOut(BaseModel):
    id: str
    organization_id: str
    source_id: str
    external_id: str
    status: Literal["pending", "shipped", "delivered"]
    last_sequence: int
    updated_at: datetime


class EffectOut(BaseModel):
    id: str
    organization_id: str
    shipment_id: str
    event_id: str
    effect_key: str
    previous_status: str
    status: str
    sequence: int
    created_at: datetime


class NotificationOut(BaseModel):
    id: str
    organization_id: str
    effect_id: str
    message: str
    created_at: datetime


class AuditOut(BaseModel):
    id: str
    organization_id: str
    actor_id: str | None
    action: str
    resource_id: str
    details: dict
    created_at: datetime


class DashboardOut(BaseModel):
    received_24h: int
    duplicate_deliveries_all_time: int
    queue_depth: int
    processing: int
    retry_wait: int
    failed: int
    succeeded: int
    ignored: int
    median_processing_ms_24h: float | None
    oldest_queued_age_seconds: int | None
    window_start: datetime
    window_end: datetime


class ReplayOut(BaseModel):
    event_id: str
    run_id: str
    run_number: int
    state: Literal["QUEUED"]


class IngestOut(BaseModel):
    id: str
    duplicate: bool
