from datetime import timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from app.api.dependencies import DB, Config
from app.core.security import utcnow
from app.models import WorkerHeartbeat

router = APIRouter()


@router.get("/health/live", tags=["Operations"])
def live():
    return {"status": "live"}


@router.get("/health/ready", tags=["Operations"])
def ready(db: DB, settings: Config):
    try:
        # A real INSERT inside a rolled-back transaction checks writable authoritative storage.
        db.execute(
            text("INSERT INTO readiness_probes (id) VALUES (1) ON CONFLICT (id) DO UPDATE SET id=1")
        )
        db.rollback()
    except Exception:
        db.rollback()
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "database": "unavailable"}
        )
    broker = "available"
    try:
        with Redis.from_url(
            settings.redis_url, socket_connect_timeout=0.3, socket_timeout=0.3
        ) as redis:
            redis.ping()
    except Exception:
        broker = "unavailable"
    heartbeat = db.get(WorkerHeartbeat, "discovery")
    worker = (
        "available"
        if heartbeat and heartbeat.seen_at > utcnow() - timedelta(seconds=30)
        else "unconfirmed"
    )
    return {
        "status": "ready",
        "database": "writable",
        "broker": broker,
        "worker_discovery": worker,
        "processing_degraded": broker != "available" or worker != "available",
    }
