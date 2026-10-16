from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select, text

from app.core.errors import AppError
from app.core.security import sign, utcnow
from app.models import BusinessEffect, DurableJob, InternalNotification, ProcessingRun
from app.schemas import ReplayInput
from app.services.ingestion import ingest
from app.services.processing import claim_job, execute_claim, process_job
from app.services.replay import replay_event
from tests.integration.test_processing import accept

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


def test_concurrent_duplicate_deliveries_use_separate_connections(tenant, factory, settings, body):
    _, source, secret, _ = tenant()
    barrier, now, raw = Barrier(8), utcnow(), body()
    ts = str(int(now.timestamp()))

    def deliver(_):
        with factory() as db:
            pid = db.scalar(text("SELECT pg_backend_pid()"))
            barrier.wait(timeout=20)
            return pid, ingest(
                db, source.public_id, raw, ts, sign(secret, ts, raw), settings, "race", now=now
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(deliver, range(8)))
    assert len({pid for pid, _ in results}) == 8
    assert sum(not result["duplicate"] for _, result in results) == 1
    assert len({result["id"] for _, result in results}) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(DurableJob)) == 1


def test_concurrent_job_publications_only_one_claim(tenant, db, factory, settings, body):
    _, source, secret, _ = tenant()
    now = utcnow()
    job = accept(db, source, secret, body(), settings, now)
    barrier = Barrier(6)

    def claim(_):
        with factory() as worker:
            worker.scalar(text("SELECT pg_backend_pid()"))
            barrier.wait(timeout=20)
            return claim_job(worker, job.id, settings, clock=lambda: now)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(claim, range(6)))
    claims = [result for result in results if result]
    assert len(claims) == 1
    assert execute_claim(db, claims[0], settings, clock=lambda: now) == "SUCCEEDED"
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == 1
    assert db.scalar(select(func.count()).select_from(InternalNotification)) == 1


def test_concurrent_same_shipment_status_has_one_notification(tenant, db, factory, settings, body):
    _, source, secret, _ = tenant()
    now = utcnow()
    jobs = [accept(db, source, secret, body(f"evt_{i}", i + 1), settings, now) for i in range(6)]
    barrier = Barrier(6)

    def process(job):
        with factory() as worker:
            worker.scalar(text("SELECT pg_backend_pid()"))
            barrier.wait(timeout=20)
            return process_job(worker, job.id, settings, clock=lambda: now)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(process, jobs))
    assert set(results) <= {"SUCCEEDED", "IGNORED"}
    assert db.scalar(select(func.count()).select_from(BusinessEffect)) == 1
    assert db.scalar(select(func.count()).select_from(InternalNotification)) == 1


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_replay_preserves_single_active_run(
    tenant, db, factory, settings, body, same_key
):
    principal, source, secret, _ = tenant()
    now = utcnow()
    job = accept(db, source, secret, body(shipment_id="unknown"), settings, now)
    assert process_job(db, job.id, settings, clock=lambda: now) == "FAILED"
    event_id = db.get(ProcessingRun, job.run_id).event_id
    barrier = Barrier(2)

    def replay(number):
        with factory() as worker:
            worker.scalar(text("SELECT pg_backend_pid()"))
            barrier.wait(timeout=20)
            try:
                return replay_event(
                    worker,
                    principal,
                    event_id,
                    ReplayInput(reason="Fixed dependency"),
                    "shared-key" if same_key else f"key-{number}",
                )
            except AppError as exc:
                assert exc.status == 409
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(replay, range(2)))
    if same_key:
        assert results[0] == results[1] and results[0] is not None
    else:
        assert sum(result is not None for result in results) == 1
    assert db.scalar(select(func.count()).select_from(ProcessingRun)) == 2
