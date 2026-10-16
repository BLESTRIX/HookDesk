import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.dependencies import (
    DB,
    Config,
    CurrentUser,
    check_origin,
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
    SourceInput,
    SourcePatch,
)
from app.services import auth, queries, sources
from app.services.replay import replay_event

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
router = APIRouter(include_in_schema=False)


async def read_form(request: Request):
    return dict(await request.form(max_fields=20, max_files=0))


FormData = Annotated[dict, Depends(read_form)]
PageNumber = Annotated[int, Query(ge=1)]


def render(request, db, principal, template, **context):
    org = db.get(Organization, principal.organization_id)
    return templates.TemplateResponse(
        request,
        template,
        {
            "principal": principal,
            "organization": org,
            "csrf": request.cookies.get("hookdesk_csrf", ""),
            "path": request.url.path,
            **context,
        },
    )


def check_form(request, form, principal, settings):
    check_origin(request, settings)
    auth.verify_csrf(principal, form.get("csrf_token"))


@router.get("/login")
@router.get("/register")
def auth_form(request: Request, settings: Config):
    token = new_token()
    response = templates.TemplateResponse(
        request,
        "auth.html",
        {
            "register": request.url.path == "/register",
            "csrf": token,
        },
    )
    response.set_cookie(
        "hookdesk_preauth",
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=600,
        path="/",
    )
    return response


@router.post("/login")
def login(request: Request, form: FormData, db: DB, settings: Config):
    verify_preauth(request, form.get("csrf_token"), settings)
    token, csrf = auth.login(db, LoginInput.model_validate(form), settings, request.client.host)
    response = RedirectResponse("/", status_code=303)
    set_session_cookies(response, token, csrf, settings)
    return response


@router.post("/register")
def register(request: Request, form: FormData, db: DB, settings: Config):
    verify_preauth(request, form.get("csrf_token"), settings)
    auth.rate_limit(db, f"register:{request.client.host}", settings)
    token, csrf = auth.register(db, RegisterInput.model_validate(form), settings)
    response = RedirectResponse("/", status_code=303)
    set_session_cookies(response, token, csrf, settings)
    return response


@router.post("/logout")
def logout(request: Request, form: FormData, db: DB, principal: CurrentUser, settings: Config):
    check_form(request, form, principal, settings)
    auth.logout(db, principal)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("hookdesk_session", path="/")
    response.delete_cookie("hookdesk_csrf", path="/")
    return response


@router.get("/")
def overview(request: Request, db: DB, principal: CurrentUser):
    template = "partials/metrics.html" if request.headers.get("HX-Request") else "overview.html"
    return render(
        request,
        db,
        principal,
        template,
        metrics=queries.dashboard(db, principal),
        recent=queries.events(db, principal, EventFilters(page_size=5)),
    )


@router.get("/sources")
def source_list(request: Request, db: DB, principal: CurrentUser, page: PageNumber = 1):
    return render(
        request, db, principal, "sources.html", result=queries.sources(db, principal, page)
    )


@router.get("/sources/new")
def source_new(request: Request, db: DB, principal: CurrentUser):
    auth.require_role(principal, "ADMIN")
    return render(request, db, principal, "source_form.html")


@router.post("/sources")
def source_create(
    request: Request, form: FormData, db: DB, principal: CurrentUser, settings: Config
):
    check_form(request, form, principal, settings)
    source, secret = sources.create_source(
        db, principal, SourceInput.model_validate(form), settings
    )
    return render(
        request,
        db,
        principal,
        "partials/secret.html" if request.headers.get("HX-Request") else "secret_result.html",
        source=source,
        secret=secret,
        base_url=settings.public_base_url,
    )


@router.get("/sources/{source_id}")
def source_detail(
    source_id: UUID, request: Request, db: DB, principal: CurrentUser, settings: Config
):
    return render(
        request,
        db,
        principal,
        "source_detail.html",
        source=owned(db, WebhookSource, source_id, principal),
        base_url=settings.public_base_url,
    )


@router.post("/sources/{source_id}")
def source_update(
    source_id: UUID,
    request: Request,
    form: FormData,
    db: DB,
    principal: CurrentUser,
    settings: Config,
):
    check_form(request, form, principal, settings)
    data = SourcePatch(name=form.get("name"), active=form.get("active") == "on")
    sources.update_source(db, principal, source_id, data)
    return RedirectResponse(f"/sources/{source_id}", status_code=303)


@router.post("/sources/{source_id}/rotate-secret")
def source_rotate(
    source_id: UUID,
    request: Request,
    form: FormData,
    db: DB,
    principal: CurrentUser,
    settings: Config,
):
    from app.schemas import RotationInput

    check_form(request, form, principal, settings)
    data = RotationInput(grace_seconds=form.get("grace_seconds") or None)
    source, secret = sources.rotate_secret(db, principal, source_id, data.grace_seconds, settings)
    return render(
        request,
        db,
        principal,
        "partials/secret.html" if request.headers.get("HX-Request") else "secret_result.html",
        source=source,
        secret=secret,
        base_url=settings.public_base_url,
    )


def inbox_filters(request, failed=False):
    values = {k: v for k, v in request.query_params.items() if v}
    if failed:
        values["state"] = "FAILED"
    for key in ("received_from", "received_to"):
        if key in values:
            try:
                date = datetime.fromisoformat(values[key])
            except ValueError:
                continue  # Pydantic supplies the useful validation error below.
            values[key] = date.replace(tzinfo=UTC) if date.tzinfo is None else date
    return EventFilters.model_validate(values)


@router.get("/events")
@router.get("/failed")
def inbox(request: Request, db: DB, principal: CurrentUser):
    failed = request.url.path == "/failed"
    filters = inbox_filters(request, failed)
    template = "partials/events.html" if request.headers.get("HX-Request") else "inbox.html"
    return render(
        request,
        db,
        principal,
        template,
        result=queries.events(db, principal, filters),
        filters=filters,
        failed=failed,
        source_options=queries.sources(db, principal, 1, 100)["items"],
    )


@router.get("/events/{event_id}")
def event_detail(
    event_id: UUID,
    request: Request,
    db: DB,
    principal: CurrentUser,
    run_page: PageNumber = 1,
    attempt_page: PageNumber = 1,
):
    event = owned(db, WebhookEvent, event_id, principal)
    return render(
        request,
        db,
        principal,
        "event_detail.html",
        event=event,
        payload=json.dumps(event.payload, indent=2, ensure_ascii=False),
        runs=queries.event_runs(db, principal, event.id, run_page),
        attempts=queries.event_attempts(db, principal, event.id, attempt_page),
    )


@router.get("/events/{event_id}/replay")
def replay_form(event_id: UUID, request: Request, db: DB, principal: CurrentUser):
    auth.require_role(principal, "ADMIN", "OPERATOR")
    return render(
        request,
        db,
        principal,
        "replay.html",
        event=owned(db, WebhookEvent, event_id, principal),
        idempotency_key=new_token(),
    )


@router.post("/events/{event_id}/replay")
def replay(
    event_id: UUID,
    request: Request,
    form: FormData,
    db: DB,
    principal: CurrentUser,
    settings: Config,
):
    check_form(request, form, principal, settings)
    result = replay_event(
        db, principal, str(event_id), ReplayInput.model_validate(form), form.get("idempotency_key")
    )
    return render(
        request,
        db,
        principal,
        "partials/replay_result.html"
        if request.headers.get("HX-Request")
        else "replay_result.html",
        result=result,
    )


@router.get("/shipments")
def shipments(request: Request, db: DB, principal: CurrentUser, page: PageNumber = 1):
    return render(
        request, db, principal, "shipments.html", result=queries.shipments(db, principal, page)
    )


@router.get("/shipments/{shipment_id}")
def shipment_detail(
    shipment_id: UUID, request: Request, db: DB, principal: CurrentUser, page: PageNumber = 1
):
    return render(
        request,
        db,
        principal,
        "shipment_detail.html",
        shipment=owned(db, Shipment, shipment_id, principal),
        result=queries.shipment_history(db, principal, shipment_id, page),
    )


@router.get("/notifications")
def notifications(request: Request, db: DB, principal: CurrentUser, page: PageNumber = 1):
    return render(
        request,
        db,
        principal,
        "activity.html",
        title="Notifications",
        subtitle="Status changes committed with your shipment updates.",
        result=queries.notifications(db, principal, page),
        audit=False,
    )


@router.get("/audit")
def audit(request: Request, db: DB, principal: CurrentUser, page: PageNumber = 1):
    return render(
        request,
        db,
        principal,
        "activity.html",
        title="Audit history",
        subtitle="Who changed configuration and requested recovery.",
        result=queries.audit_events(db, principal, page),
        audit=True,
    )
