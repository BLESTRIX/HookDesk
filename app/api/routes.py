from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.api.dependencies import (
    DB,
    Config,
    CurrentUser,
    protect_write,
    set_session_cookies,
    verify_preauth,
)
from app.core.security import new_token
from app.models import Organization, Shipment, WebhookEvent, WebhookSource
from app.repositories.common import owned
from app.schemas import (
    EventFilters,
    LoginInput,
    RegisterInput,
    ReplayInput,
    RotationInput,
    SourceInput,
    SourcePatch,
)
from app.schemas.responses import (
    AttemptOut,
    AuditOut,
    DashboardOut,
    EffectOut,
    EventDetail,
    EventOut,
    NotificationOut,
    Page,
    ReplayOut,
    RunOut,
    ShipmentOut,
    SourceCreated,
    SourceOut,
)
from app.services import auth, queries, sources
from app.services.replay import replay_event

router = APIRouter(prefix="/api/v1")


@router.get("/auth/csrf", tags=["Authentication"])
def csrf_challenge(response: Response, settings: Config):
    token = new_token()
    response.set_cookie(
        "hookdesk_preauth",
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=600,
        path="/",
    )
    return {"csrf_token": token}


@router.post("/auth/register", status_code=201, tags=["Authentication"])
def register(data: RegisterInput, request: Request, response: Response, db: DB, settings: Config):
    verify_preauth(request, request.headers.get("X-CSRF-Token"), settings)
    auth.rate_limit(db, f"register:{request.client.host}", settings)
    token, csrf = auth.register(db, data, settings)
    set_session_cookies(response, token, csrf, settings)
    return {"csrf_token": csrf}


@router.post("/auth/login", tags=["Authentication"])
def login(data: LoginInput, request: Request, response: Response, db: DB, settings: Config):
    verify_preauth(request, request.headers.get("X-CSRF-Token"), settings)
    token, csrf = auth.login(db, data, settings, request.client.host)
    set_session_cookies(response, token, csrf, settings)
    return {"csrf_token": csrf}


@router.post("/auth/logout", dependencies=[Depends(protect_write)], tags=["Authentication"])
def logout(response: Response, db: DB, principal: CurrentUser):
    auth.logout(db, principal)
    response.delete_cookie("hookdesk_session", path="/")
    response.delete_cookie("hookdesk_csrf", path="/")
    return {"logged_out": True}


@router.get("/auth/me", tags=["Authentication"])
def me(db: DB, principal: CurrentUser):
    org = db.get(Organization, principal.organization_id)
    return {
        "id": principal.user_id,
        "email": principal.email,
        "role": principal.role,
        "organization_id": principal.organization_id,
        "organization_name": org.name,
    }


Pagination = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


@router.get("/sources", response_model=Page[SourceOut], tags=["Sources"])
def list_sources(db: DB, principal: CurrentUser, page: Pagination = 1, page_size: PageSize = 25):
    return queries.sources(db, principal, page, page_size)


@router.post(
    "/sources",
    response_model=SourceCreated,
    status_code=201,
    dependencies=[Depends(protect_write)],
    tags=["Sources"],
)
def create_source(data: SourceInput, db: DB, principal: CurrentUser, settings: Config):
    source, secret = sources.create_source(db, principal, data, settings)
    return {**queries.serialize(source), "signing_secret": secret}


@router.get("/sources/{source_id}", response_model=SourceOut, tags=["Sources"])
def get_source(source_id: UUID, db: DB, principal: CurrentUser):
    return queries.serialize(owned(db, WebhookSource, source_id, principal))


@router.patch(
    "/sources/{source_id}",
    response_model=SourceOut,
    dependencies=[Depends(protect_write)],
    tags=["Sources"],
)
def patch_source(source_id: UUID, data: SourcePatch, db: DB, principal: CurrentUser):
    return queries.serialize(sources.update_source(db, principal, source_id, data))


@router.post(
    "/sources/{source_id}/rotate-secret",
    response_model=SourceCreated,
    dependencies=[Depends(protect_write)],
    tags=["Sources"],
)
def rotate(source_id: UUID, data: RotationInput, db: DB, principal: CurrentUser, settings: Config):
    source, secret = sources.rotate_secret(db, principal, source_id, data.grace_seconds, settings)
    return {**queries.serialize(source), "signing_secret": secret}


@router.get("/events", response_model=Page[EventOut], tags=["Events"])
def list_events(db: DB, principal: CurrentUser, filters: Annotated[EventFilters, Query()]):
    return queries.events(db, principal, filters)


@router.get("/events/{event_id}", response_model=EventDetail, tags=["Events"])
def get_event(event_id: UUID, db: DB, principal: CurrentUser):
    return queries.serialize(owned(db, WebhookEvent, event_id, principal))


@router.get("/events/{event_id}/runs", response_model=Page[RunOut], tags=["Events"])
def runs(
    event_id: UUID, db: DB, principal: CurrentUser, page: Pagination = 1, page_size: PageSize = 25
):
    return queries.event_runs(db, principal, event_id, page, page_size)


@router.get("/events/{event_id}/attempts", response_model=Page[AttemptOut], tags=["Events"])
def attempts(
    event_id: UUID, db: DB, principal: CurrentUser, page: Pagination = 1, page_size: PageSize = 25
):
    return queries.event_attempts(db, principal, event_id, page, page_size)


@router.post(
    "/events/{event_id}/replay",
    response_model=ReplayOut,
    status_code=202,
    dependencies=[Depends(protect_write)],
    tags=["Events"],
)
def replay(
    event_id: UUID,
    data: ReplayInput,
    db: DB,
    principal: CurrentUser,
    idempotency_key: Annotated[str | None, Header()] = None,
):
    return replay_event(db, principal, str(event_id), data, idempotency_key)


@router.get("/shipments", response_model=Page[ShipmentOut], tags=["Shipments"])
def list_shipments(db: DB, principal: CurrentUser, page: Pagination = 1, page_size: PageSize = 25):
    return queries.shipments(db, principal, page, page_size)


@router.get("/shipments/{shipment_id}", response_model=ShipmentOut, tags=["Shipments"])
def get_shipment(shipment_id: UUID, db: DB, principal: CurrentUser):
    return queries.serialize(owned(db, Shipment, shipment_id, principal))


@router.get("/shipments/{shipment_id}/history", response_model=Page[EffectOut], tags=["Shipments"])
def history(
    shipment_id: UUID,
    db: DB,
    principal: CurrentUser,
    page: Pagination = 1,
    page_size: PageSize = 25,
):
    return queries.shipment_history(db, principal, shipment_id, page, page_size)


@router.get("/dashboard", response_model=DashboardOut, tags=["Operations"])
def dashboard(db: DB, principal: CurrentUser):
    return queries.dashboard(db, principal)


@router.get("/notifications", response_model=Page[NotificationOut], tags=["Operations"])
def notifications(db: DB, principal: CurrentUser, page: Pagination = 1, page_size: PageSize = 25):
    return queries.notifications(db, principal, page, page_size)


@router.get("/audit-events", response_model=Page[AuditOut], tags=["Operations"])
def audit(db: DB, principal: CurrentUser, page: Pagination = 1, page_size: PageSize = 25):
    return queries.audit_events(db, principal, page, page_size)
