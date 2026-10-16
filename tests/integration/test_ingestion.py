from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.core.errors import AppError
from app.core.security import sign, utcnow
from app.models import DurableJob, EncryptedSigningSecret, WebhookEvent
from app.schemas import SourcePatch
from app.services.ingestion import ingest
from app.services.sources import rotate_secret, update_source
from tests.conftest import headers

pytestmark = pytest.mark.integration


def test_duplicate_and_changed_body(tenant, client, body, db):
    _, source, secret, _ = tenant()
    raw = body()
    url = f"/webhooks/{source.public_id}"
    first = client.post(url, content=raw, headers=headers(secret, raw))
    assert first.status_code == 202
    for _ in range(5):
        duplicate = client.post(url, content=raw, headers=headers(secret, raw))
        assert duplicate.json() == {"id": first.json()["id"], "duplicate": True}
    changed = body(sequence=2)
    assert client.post(url, content=changed, headers=headers(secret, changed)).status_code == 409
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1
    assert db.scalar(select(func.count()).select_from(DurableJob)) == 1


def test_verify_before_parsing_and_for_duplicates(tenant, client, body):
    _, source, secret, _ = tenant()
    url, raw = f"/webhooks/{source.public_id}", body()
    assert client.post(url, content=raw, headers=headers(secret, raw)).status_code == 202
    assert client.post(url, content=raw, headers=headers("wrong", raw)).status_code == 401
    malformed = b"not-json"
    assert (
        client.post(url, content=malformed, headers=headers("wrong", malformed)).status_code == 401
    )
    assert (
        client.post(url, content=malformed, headers=headers(secret, malformed)).status_code == 422
    )
    spaced = raw.replace(b":", b": ")
    assert client.post(url, content=spaced, headers=headers(secret, raw)).status_code == 401


@pytest.mark.parametrize("offset", [-301, 301])
def test_stale_and_future_delivery(tenant, client, body, offset):
    _, source, secret, _ = tenant()
    raw = body()
    assert (
        client.post(
            f"/webhooks/{source.public_id}",
            content=raw,
            headers=headers(secret, raw, offset=offset),
        ).status_code
        == 401
    )


def test_size_limit_without_or_with_false_length(tenant, client, settings):
    _, source, _, _ = tenant()
    for extra in ({}, {"Content-Length": "1"}):
        response = client.post(
            f"/webhooks/{source.public_id}",
            content=iter([b"x" * 200000, b"x" * 100000]),
            headers={"Content-Type": "application/json", **extra},
        )
        assert response.status_code == 413


@pytest.mark.parametrize(
    "data",
    [b'{"event_id":"a","event_id":"b"}', b'{"data":{"status":"shipped","status":"delivered"}}'],
)
def test_duplicate_json_keys_not_accepted(tenant, client, data, db):
    _, source, secret, _ = tenant()
    result = client.post(
        f"/webhooks/{source.public_id}", content=data, headers=headers(secret, data)
    )
    assert result.status_code == 422
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_rotation_grace_and_deactivation(tenant, db, settings, body):
    principal, source, old_secret, _ = tenant()
    _, new_secret = rotate_secret(db, principal, source.id, 100, settings)
    assert len(list(db.scalars(select(EncryptedSigningSecret)))) == 2
    now = utcnow()
    for secret in (old_secret, new_secret):
        ts, raw = str(int(now.timestamp())), body("evt_" + secret[:6])
        assert not ingest(
            db, source.public_id, raw, ts, sign(secret, ts, raw), settings, "test", now=now
        )["duplicate"]
    later = now + timedelta(seconds=101)
    raw, ts = body("expired"), str(int(later.timestamp()))
    with pytest.raises(AppError) as exc:
        ingest(
            db, source.public_id, raw, ts, sign(old_secret, ts, raw), settings, "test", now=later
        )
    assert exc.value.code == "invalid_signature"
    db.rollback()
    assert not ingest(
        db, source.public_id, raw, ts, sign(new_secret, ts, raw), settings, "test", now=later
    )["duplicate"]
    update_source(db, principal, source.id, SourcePatch(active=False))
    with pytest.raises(AppError) as exc:
        ingest(
            db, source.public_id, raw, ts, sign(new_secret, ts, raw), settings, "test", now=later
        )
    assert exc.value.code == "source_unavailable"


def test_persistence_failure_is_503(tenant, client, body, factory, monkeypatch, db):
    _, source, secret, _ = tenant()
    raw = body()

    def fail(self):
        raise OperationalError("commit", None, Exception("unavailable"))

    monkeypatch.setattr(factory.class_, "commit", fail)
    response = client.post(
        f"/webhooks/{source.public_id}", content=raw, headers=headers(secret, raw)
    )
    assert response.status_code == 503
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 0


def test_acknowledgment_waits_for_commit(tenant, client, body, factory, monkeypatch):
    _, source, secret, _ = tenant()
    raw, entered, release = body(), Event(), Event()
    original = factory.class_.commit

    def blocking_commit(self):
        if any(isinstance(obj, DurableJob) for obj in self.new):
            entered.set()
            assert release.wait(10)
        return original(self)

    monkeypatch.setattr(factory.class_, "commit", blocking_commit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post, f"/webhooks/{source.public_id}", content=raw, headers=headers(secret, raw)
        )
        try:
            assert entered.wait(10)
            with factory() as observer:
                assert observer.scalar(select(func.count()).select_from(WebhookEvent)) == 0
                assert observer.scalar(select(func.count()).select_from(DurableJob)) == 0
            assert not future.done()
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 202


def test_broker_unavailable_does_not_block_ingestion(tenant, client, body, monkeypatch):
    from celery import Celery
    from redis import Redis

    def unavailable(*args, **kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(Celery, "send_task", unavailable)
    monkeypatch.setattr(Redis, "execute_command", unavailable)
    _, source, secret, _ = tenant()
    raw = body()
    assert (
        client.post(
            f"/webhooks/{source.public_id}", content=raw, headers=headers(secret, raw)
        ).status_code
        == 202
    )
    readiness = client.get("/health/ready")
    assert readiness.status_code == 200 and readiness.json()["processing_degraded"]
