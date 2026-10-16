import hmac
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import digest
from app.db.session import get_db
from app.services.auth import Principal, authenticate, verify_csrf

DB = Annotated[Session, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]


def get_principal(request: Request, db: DB):
    return authenticate(db, request.cookies.get("hookdesk_session"))


CurrentUser = Annotated[Principal, Depends(get_principal)]


def protect_write(request: Request, principal: CurrentUser):
    verify_csrf(principal, request.headers.get("X-CSRF-Token"))


def check_origin(request, settings):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.public_base_url.rstrip("/"):
        raise AppError(403, "origin_denied", "This request came from an unexpected origin")


def verify_preauth(request, supplied, settings):
    check_origin(request, settings)
    cookie = request.cookies.get("hookdesk_preauth")
    if not cookie or not supplied or not hmac.compare_digest(digest(cookie), digest(supplied)):
        raise AppError(403, "csrf_failed", "Load a fresh authentication form before submitting")


def set_session_cookies(response, token, csrf, settings):
    options = dict(
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_hours * 3600,
    )
    response.set_cookie("hookdesk_session", token, httponly=True, **options)
    response.set_cookie("hookdesk_csrf", csrf, httponly=False, **options)
    response.delete_cookie("hookdesk_preauth", path="/")
