import logging

from fastapi import APIRouter, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import DB, Config
from app.core.errors import AppError
from app.schemas.responses import IngestOut
from app.services.ingestion import increment_counter, ingest

router = APIRouter()


async def bounded_body(request, limit):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise AppError(413, "body_too_large", f"Webhook bodies must be at most {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def count_rejection(db, code):
    try:
        db.rollback()
        increment_counter(db, code)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
    logging.getLogger(__name__).info("webhook.rejected", extra={"error_code": code})


@router.post(
    "/webhooks/{public_source_id}",
    status_code=202,
    response_model=IngestOut,
    tags=["Public ingestion"],
    description="Sign ASCII(timestamp) + b'.' + exact raw JSON bytes using HMAC-SHA256. "
    "Tolerance ±300 seconds; maximum body 256 KiB by default. Retries must preserve "
    "the body bytes and generate a fresh timestamp/signature. Duplicates also return 202.",
    openapi_extra={
        "parameters": [
            {
                "name": "X-HookDesk-Timestamp",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "description": "Unix timestamp in seconds"},
            },
            {
                "name": "X-HookDesk-Signature",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "pattern": "^sha256=[a-f0-9]{64}$"},
            },
        ],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/CourierEvent"},
                    "example": {
                        "event_id": "evt_demo_001",
                        "event_type": "shipment.status_changed",
                        "occurred_at": "2026-10-16T03:00:00Z",
                        "data": {"shipment_id": "SHIP-1001", "sequence": 1, "status": "shipped"},
                    },
                }
            },
        },
    },
)
async def receive(public_source_id: str, request: Request, db: DB, settings: Config):
    try:
        if (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            raise AppError(415, "unsupported_media_type", "Use Content-Type: application/json")
        body = await bounded_body(request, settings.max_body_bytes)
        return await run_in_threadpool(
            ingest,
            db,
            public_source_id,
            body,
            request.headers.get("X-HookDesk-Timestamp", ""),
            request.headers.get("X-HookDesk-Signature", ""),
            settings,
            request.state.request_id,
        )
    except AppError as exc:
        await run_in_threadpool(count_rejection, db, exc.code)
        raise
    except SQLAlchemyError:
        await run_in_threadpool(db.rollback)
        raise AppError(
            503, "persistence_unavailable", "Could not persist this event; retry delivery"
        ) from None
