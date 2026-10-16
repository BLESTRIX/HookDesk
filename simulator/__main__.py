import argparse
import asyncio
import getpass
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.core.security import sign


def event_body(event_id, shipment_id, sequence=1, status="shipped"):
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": "shipment.status_changed",
            "occurred_at": datetime.now(UTC).isoformat(),
            "data": {
                "shipment_id": shipment_id,
                "sequence": sequence,
                "status": status,
            },
        },
        separators=(",", ":"),
    ).encode()


async def run(args, secret):
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:

        async def send(body, mode=None):
            async with semaphore:
                timestamp = str(int(time.time()) - (600 if mode == "stale" else 0))
                signature = sign(secret, timestamp, body)
                if mode == "invalid":
                    signature = "sha256=" + "0" * 64
                response = await client.post(
                    args.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-HookDesk-Timestamp": timestamp,
                        "X-HookDesk-Signature": signature,
                    },
                )
                print(f"HTTP {response.status_code}: {response.text[:400]}")
                expected = (
                    401 if mode in {"invalid", "stale"} else 409 if mode == "conflict" else 202
                )
                return response.status_code == expected

        body = (
            Path(args.body_file).read_bytes()
            if args.body_file
            else event_body(args.event_id, args.shipment, args.sequence, args.status)
        )
        if args.mode == "out-of-order":
            # Send the latest business state first. Worker arrival order is independently safe.
            outcomes = [
                await send(event_body(args.event_id + "_latest", args.shipment, 2, "delivered")),
                await send(event_body(args.event_id + "_older", args.shipment, 1, "shipped")),
            ]
        elif args.mode == "conflict":
            accepted = await send(body)
            changed = json.loads(body)
            changed["data"]["sequence"] += 1
            outcomes = [accepted, await send(json.dumps(changed).encode(), "conflict")]
        elif args.mode == "batch":
            outcomes = await asyncio.gather(
                *(
                    send(
                        event_body(
                            f"{args.event_id}_{i}", args.shipment, args.sequence + i, args.status
                        )
                    )
                    for i in range(args.count)
                )
            )
        elif args.mode == "concurrent":
            outcomes = await asyncio.gather(*(send(body) for _ in range(args.count)))
        elif args.mode == "duplicate":
            outcomes = [await send(body) for _ in range(args.count)]
        else:
            outcomes = [await send(body, args.mode)]
        if not all(outcomes):
            raise SystemExit("A response did not match the selected scenario")


def main():
    parser = argparse.ArgumentParser(
        description="Send signed courier deliveries without printing signing secrets"
    )
    parser.add_argument(
        "mode",
        choices=[
            "valid",
            "duplicate",
            "invalid",
            "stale",
            "conflict",
            "out-of-order",
            "concurrent",
            "batch",
        ],
    )
    parser.add_argument("--url", required=True, help="Full /webhooks/{public_source_id} endpoint")
    parser.add_argument("--shipment", default="SHIP-1001")
    parser.add_argument("--event-id", default="evt_" + uuid.uuid4().hex[:16])
    parser.add_argument("--sequence", type=int, default=1)
    parser.add_argument("--status", choices=["shipped", "delivered"], default="shipped")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--body-file", help="Reuse exact UTF-8 body bytes across CLI invocations")
    args = parser.parse_args()
    if not (1 <= args.count <= 1000 and 1 <= args.concurrency <= 25 and args.sequence > 0):
        parser.error("Use count 1–1000, concurrency 1–25, and a positive sequence")
    if not args.url.startswith(("http://", "https://")):
        parser.error("Use an HTTP(S) endpoint")
    secret = os.getenv("HOOKDESK_SIGNING_SECRET") or getpass.getpass("Signing secret: ")
    if not secret:
        parser.error("A signing secret is required")
    asyncio.run(run(args, secret))


if __name__ == "__main__":
    main()
