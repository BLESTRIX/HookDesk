import logging
import os

from celery import Celery
from celery.signals import setup_logging
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import utcnow
from app.db.session import session_factory
from app.models import WorkerHeartbeat
from app.services.processing import SimulatedCrash, due_jobs, process_job

settings = get_settings()
celery = Celery("hookdesk", broker=settings.redis_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=2,
    broker_transport_options={"socket_timeout": 2, "socket_connect_timeout": 2},
    task_publish_retry=False,
    beat_schedule={
        "discover-durable-work": {
            "task": "hookdesk.discover",
            "schedule": settings.discovery_seconds,
        }
    },
)


@setup_logging.connect
def logging_setup(**kwargs):
    configure_logging()


@celery.task(name="hookdesk.process")
def process(job_id):
    with session_factory()() as db:
        try:
            return process_job(db, job_id, settings)
        except SimulatedCrash:
            # Prefork child only; the supervisor replaces it and the lease is recovered by discovery.
            os._exit(72)


@celery.task(name="hookdesk.discover")
def discover():
    with session_factory()() as db:
        db.execute(
            insert(WorkerHeartbeat)
            .values(name="discovery", seen_at=utcnow())
            .on_conflict_do_update(
                index_elements=[WorkerHeartbeat.name], set_={"seen_at": utcnow()}
            )
        )
        jobs = due_jobs(db)
        db.commit()
    for job_id in jobs:
        try:
            process.apply_async(args=[job_id], retry=False)
        except Exception:
            logging.getLogger(__name__).warning("broker.publish_failed", extra={"job_id": job_id})
            break  # Remains due in PostgreSQL; the next discovery pass tries again.
    return len(jobs)
