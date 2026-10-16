"""Exercise real web, PostgreSQL, Redis, worker, and Beat processes in a demo deployment."""

import argparse
import getpass
import os
import subprocess
import time
import uuid

import httpx
from dotenv import load_dotenv
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.security import sign
from app.db.session import session_factory
from app.models import DemoFault, DurableJob, ProcessingRun, WebhookSource
from app.repositories.common import owned
from app.services.auth import Principal, require_role
from app.services.management import create_shipment
from simulator.__main__ import event_body


def expect(response, status=200):
    assert response.status_code == status, f"HTTP {response.status_code}: {response.text[:300]}"
    return response.json()


def wait_for(client, event_id, wanted, timeout=90):
    deadline, previous = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        event = expect(client.get(f"/api/v1/events/{event_id}"))
        if event["state"] != previous:
            print(f"  {event['external_event_id']}: {event['state']}")
            previous = event["state"]
        if event["state"] == wanted:
            return event
        if event["state"] == "FAILED" and wanted != "FAILED":
            raise AssertionError("Unexpected permanent failure")
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {wanted}; check worker and Beat logs")


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument(
        "--crash-recovery",
        action="store_true",
        help="Terminate one demo worker child and verify lease recovery",
    )
    parser.add_argument(
        "--redis-outage",
        action="store_true",
        help="Temporarily stop and restart this project's Redis service",
    )
    args = parser.parse_args()
    settings = get_settings()
    if not settings.demo_enabled or settings.environment == "production":
        raise SystemExit(
            "Use a development deployment with DEMO_ENABLED=true and compose.demo.yaml"
        )
    password = os.getenv("DEMO_PASSWORD") or getpass.getpass("Demo admin password: ")
    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        csrf = expect(client.get("/api/v1/auth/csrf"))["csrf_token"]
        token = expect(
            client.post(
                "/api/v1/auth/login",
                json={"email": args.email, "password": password},
                headers={"X-CSRF-Token": csrf},
            )
        )["csrf_token"]
        client.headers["X-CSRF-Token"] = token
        me = expect(client.get("/api/v1/auth/me"))
        principal = Principal(me["organization_id"], me["id"], me["role"], me["email"], "", "")
        require_role(principal, "ADMIN")
        unique = uuid.uuid4().hex[:10]
        source = expect(
            client.post("/api/v1/sources", json={"name": f"Recovery demo {unique}"}), 201
        )
        secret = source.pop("signing_secret")
        endpoint = f"/webhooks/{source['public_id']}"
        with session_factory()() as db:
            owned(db, WebhookSource, source["id"], principal)
            shipment = create_shipment(db, principal, source["id"], "SHIP-1001")
            create_shipment(db, principal, source["id"], "SHIP-1002")
            crash_shipment = create_shipment(db, principal, source["id"], "SHIP-1003")
            db.add(
                DemoFault(
                    organization_id=principal.organization_id,
                    source_id=source["id"],
                    mode="timeout",
                    remaining=100,
                )
            )
            db.commit()
        before = expect(client.get("/api/v1/notifications"))["total"]

        def deliver(raw):
            timestamp = str(int(time.time()))
            return expect(
                client.post(
                    endpoint,
                    content=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-HookDesk-Timestamp": timestamp,
                        "X-HookDesk-Signature": sign(secret, timestamp, raw),
                    },
                ),
                202,
            )

        raw = event_body(f"evt_recovery_{unique}", "SHIP-1001")
        print("1. Accept signed event; force dependency timeouts")
        event_id = deliver(raw)["id"]
        failed = wait_for(client, event_id, "FAILED")
        runs = expect(client.get(f"/api/v1/events/{event_id}/runs"))["items"]
        assert runs[0]["attempt_count"] == settings.max_attempts
        failed_ids = {
            e["id"]
            for e in expect(
                client.get("/api/v1/events", params={"state": "FAILED", "source_id": source["id"]})
            )["items"]
        }
        assert event_id in failed_ids
        print("2. Attempt budget exhausted; event is in failed queue")
        with session_factory()() as db:
            db.execute(
                delete(DemoFault).where(
                    DemoFault.organization_id == principal.organization_id,
                    DemoFault.source_id == source["id"],
                )
            )
            db.commit()
        print("3. Remove fault; replay immutable event")
        replay = expect(
            client.post(
                f"/api/v1/events/{event_id}/replay",
                json={"reason": "Removed demo dependency outage"},
                headers={"Idempotency-Key": f"demo-{unique}"},
            ),
            202,
        )
        success = wait_for(client, event_id, "SUCCEEDED")
        assert success["body_hash"] == failed["body_hash"]
        assert replay["run_number"] == 2
        assert expect(client.get(f"/api/v1/shipments/{shipment.id}/history"))["total"] == 1
        assert expect(client.get("/api/v1/notifications"))["total"] == before + 1
        print("4. One shipment change, one notification; both runs preserved")
        for _ in range(5):
            assert deliver(raw)["duplicate"]
        with session_factory()() as db:
            jobs = list(
                db.scalars(
                    select(DurableJob)
                    .join(ProcessingRun, ProcessingRun.id == DurableJob.run_id)
                    .where(
                        ProcessingRun.organization_id == principal.organization_id,
                        ProcessingRun.event_id == event_id,
                    )
                )
            )
            assert len(jobs) == 2  # Original run and replay; duplicates create no job.
        assert expect(client.get(f"/api/v1/shipments/{shipment.id}/history"))["total"] == 1
        assert expect(client.get("/api/v1/notifications"))["total"] == before + 1
        print("5. Five identical deliveries: no additional job or business effect")

        if args.redis_outage:
            command = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.demo.yaml"]
            print("6. Stop Redis; accept a second event")
            try:
                subprocess.run([*command, "stop", "redis"], check=True)
                second_id = deliver(event_body(f"evt_outage_{unique}", "SHIP-1002"))["id"]
                assert expect(client.get(f"/api/v1/events/{second_id}"))["state"] == "QUEUED"
                health = expect(client.get("/health/ready"))
                assert health["status"] == "ready" and health["broker"] == "unavailable"
            finally:
                subprocess.run([*command, "start", "redis"], check=True)
            print("7. Redis recovered; Beat discovers durable work automatically")
            wait_for(client, second_id, "SUCCEEDED")
        if args.crash_recovery:
            print("8. Crash one worker child after claim; recover its expired lease")
            with session_factory()() as db:
                db.add(
                    DemoFault(
                        organization_id=principal.organization_id,
                        source_id=source["id"],
                        mode="crash",
                        remaining=1,
                    )
                )
                db.commit()
            crash_id = deliver(event_body(f"evt_crash_{unique}", "SHIP-1003"))["id"]
            wait_for(client, crash_id, "SUCCEEDED")
            attempts = expect(client.get(f"/api/v1/events/{crash_id}/attempts"))["items"]
            assert {attempt["outcome"] for attempt in attempts} == {"ABANDONED", "SUCCEEDED"}
            assert (
                expect(client.get(f"/api/v1/shipments/{crash_shipment.id}/history"))["total"] == 1
            )
            print("  Abandoned attempt preserved; replacement worker committed one status change")
        print(f"Demo passed. Inspect {args.base_url}/events/{event_id} in the admin workspace.")


if __name__ == "__main__":
    main()
