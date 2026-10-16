import logging
import random
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError

from app.core.errors import LostLease, PermanentFailure, TemporaryFailure
from app.core.security import utcnow
from app.db.base import new_id
from app.handlers.shipments import apply_shipment_event
from app.models import DemoFault, DurableJob, ProcessingAttempt, ProcessingRun, WebhookEvent

logger = logging.getLogger(__name__)
TERMINAL = {"SUCCEEDED", "IGNORED", "FAILED"}


class SimulatedCrash(BaseException):
    """Only the development CLI can configure this fault, never a webhook payload."""


@dataclass(frozen=True)
class Claim:
    job_id: str
    owner_token: str
    run_id: str
    event_id: str


def database_now(db, clock):
    # PostgreSQL now() is fixed at transaction start; clock_timestamp() is not.
    return clock() if clock else db.scalar(select(func.clock_timestamp()))


def due_jobs(db, *, now=None, limit=200):
    now = now or utcnow()
    return list(
        db.scalars(
            select(DurableJob.id)
            .where(
                DurableJob.completed.is_(False),
                DurableJob.available_at <= now,
                or_(DurableJob.lease_until.is_(None), DurableJob.lease_until <= now),
            )
            .order_by(DurableJob.available_at, DurableJob.id)
            .limit(limit)
        )
    )


def finish_attempt(attempt, now, outcome, code=None, summary=None):
    attempt.completed_at, attempt.outcome = now, outcome
    attempt.duration_ms = max(0, (now - attempt.started_at).total_seconds() * 1000)
    attempt.error_code, attempt.error_summary = code, summary


def set_state(event, run, state):
    event.state = run.state = state


def claim_job(db, job_id, settings, *, clock=None):
    now = database_now(db, clock)
    job = db.scalar(
        select(DurableJob).where(DurableJob.id == job_id).with_for_update(skip_locked=True)
    )
    if (
        not job
        or job.completed
        or job.available_at > now
        or (job.lease_until and job.lease_until > now)
    ):
        db.rollback()
        return None
    run = db.get(ProcessingRun, job.run_id)
    event = db.get(WebhookEvent, run.event_id)
    if run.state in TERMINAL:
        job.completed = True
        db.commit()
        return None
    if job.owner_token:
        previous = db.scalar(
            select(ProcessingAttempt).where(
                ProcessingAttempt.run_id == run.id, ProcessingAttempt.owner_token == job.owner_token
            )
        )
        if previous and previous.completed_at is None:
            finish_attempt(
                previous,
                now,
                "ABANDONED",
                "lease_expired",
                "Worker lease expired before completion",
            )
    if run.attempt_count >= settings.max_attempts:
        set_state(event, run, "FAILED")
        run.error_code, run.error_summary = (
            "attempts_exhausted",
            "Processing attempt budget exhausted",
        )
        run.completed_at, run.next_retry_at = now, None
        job.completed, job.owner_token, job.lease_until = True, None, None
        db.commit()
        return None
    token = new_id()
    job.owner_token, job.lease_until = token, now + timedelta(seconds=settings.lease_seconds)
    run.attempt_count += 1
    run.started_at = run.started_at or now
    run.next_retry_at = None
    set_state(event, run, "PROCESSING")
    db.add(
        ProcessingAttempt(
            organization_id=job.organization_id,
            run_id=run.id,
            number=run.attempt_count,
            owner_token=token,
            started_at=now,
        )
    )
    claim = Claim(job.id, token, run.id, event.id)
    db.commit()
    logger.info(
        "job.claimed",
        extra={
            "job_id": job.id,
            "run_id": run.id,
            "event_id": event.id,
            "correlation_id": event.correlation_id,
        },
    )
    return claim


def lock_owned_job(db, claim, clock):
    job = db.scalar(
        select(DurableJob)
        .where(DurableJob.id == claim.job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = database_now(db, clock)
    if (
        not job
        or job.completed
        or job.owner_token != claim.owner_token
        or not job.lease_until
        or job.lease_until <= now
    ):
        raise LostLease()
    return job, now


def consume_demo_fault(db, claim, settings, clock):
    if not settings.demo_enabled or settings.environment == "production":
        return None
    job, _ = lock_owned_job(db, claim, clock)
    event = db.get(WebhookEvent, claim.event_id)
    fault = db.scalar(
        select(DemoFault)
        .where(
            DemoFault.organization_id == job.organization_id, DemoFault.source_id == event.source_id
        )
        .with_for_update()
    )
    mode = None
    if fault and fault.remaining > 0:
        fault.remaining -= 1
        mode = fault.mode
    db.commit()  # Fault consumption survives handler rollback / simulated process death.
    return mode


def retry_delay(attempt, settings, random_fraction=None):
    ceiling = min(settings.retry_max_seconds, settings.retry_base_seconds * 2 ** (attempt - 1))
    fraction = random.random() if random_fraction is None else random_fraction
    return ceiling * (0.5 + fraction / 2)  # bounded jitter, never zero


def record_failure(db, claim, settings, code, summary, temporary, clock):
    job, now = lock_owned_job(db, claim, clock)
    run = db.get(ProcessingRun, claim.run_id)
    event = db.get(WebhookEvent, claim.event_id)
    attempt = db.scalar(
        select(ProcessingAttempt).where(
            ProcessingAttempt.run_id == run.id, ProcessingAttempt.owner_token == claim.owner_token
        )
    )
    retry = temporary and run.attempt_count < settings.max_attempts
    state = "RETRY_WAIT" if retry else "FAILED"
    finish_attempt(attempt, now, state, code, summary)
    set_state(event, run, state)
    run.error_code, run.error_summary = code, summary
    job.owner_token = job.lease_until = None
    if retry:
        job.available_at = now + timedelta(seconds=retry_delay(run.attempt_count, settings))
        run.next_retry_at = job.available_at
    else:
        job.completed, run.completed_at, run.next_retry_at = True, now, None
    db.commit()
    logger.info(
        "job." + state.lower(),
        extra={
            "event_id": event.id,
            "job_id": job.id,
            "run_id": run.id,
            "error_code": code,
            "correlation_id": event.correlation_id,
        },
    )
    return state


def execute_claim(db, claim, settings, *, clock=None):
    try:
        fault = consume_demo_fault(db, claim, settings, clock)
        if fault == "crash":
            raise SimulatedCrash()
        if fault == "timeout":
            raise TemporaryFailure()
        job, now = lock_owned_job(db, claim, clock)
        run = db.get(ProcessingRun, claim.run_id)
        event = db.get(WebhookEvent, claim.event_id)
        if event.event_type != "shipment.status_changed":
            raise PermanentFailure("unsupported_operation", "No handler for this event type")
        outcome = apply_shipment_event(db, event, now)
        # Job row stays locked through business effects, fencing check, and commit.
        _, finished = lock_owned_job(db, claim, clock)
        attempt = db.scalar(
            select(ProcessingAttempt).where(
                ProcessingAttempt.run_id == run.id,
                ProcessingAttempt.owner_token == claim.owner_token,
            )
        )
        finish_attempt(attempt, finished, outcome)
        set_state(event, run, outcome)
        run.completed_at, run.next_retry_at = finished, None
        run.error_code = run.error_summary = None
        job.completed, job.owner_token, job.lease_until = True, None, None
        db.commit()
        logger.info(
            "job." + outcome.lower(),
            extra={
                "job_id": job.id,
                "run_id": run.id,
                "event_id": event.id,
                "correlation_id": event.correlation_id,
            },
        )
        return outcome
    except LostLease:
        db.rollback()
        return "LOST_LEASE"
    except (PermanentFailure, TemporaryFailure, DBAPIError) as exc:
        db.rollback()  # No partial shipment effects survive a handler error.
        permanent = isinstance(exc, PermanentFailure)
        code = (
            exc.code
            if permanent
            else (
                "dependency_timeout" if isinstance(exc, TemporaryFailure) else "database_transient"
            )
        )
        summary = exc.summary if permanent else "Temporary processing failure"
        try:
            return record_failure(db, claim, settings, code, summary, not permanent, clock)
        except LostLease:
            db.rollback()
            return "LOST_LEASE"
    except Exception:
        db.rollback()
        logger.error(
            "handler.unexpected_error",
            extra={
                "job_id": claim.job_id,
                "event_id": claim.event_id,
                "run_id": claim.run_id,
                "error_code": "handler_error",
            },
        )
        try:
            return record_failure(
                db,
                claim,
                settings,
                "handler_error",
                "Unexpected handler failure; contact an administrator",
                False,
                clock,
            )
        except LostLease:
            db.rollback()
            return "LOST_LEASE"


def process_job(db, job_id, settings, *, clock=None):
    claim = claim_job(db, job_id, settings, clock=clock)
    return execute_claim(db, claim, settings, clock=clock) if claim else "NOT_CLAIMED"
