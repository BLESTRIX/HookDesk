import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.api import health, routes, webhooks
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, request_id_var
from app.schemas import CourierEvent, ErrorResponse
from app.web.routes import router as web_router
from app.web.routes import templates


@asynccontextmanager
async def lifespan(app):
    get_settings()  # Fail at startup when encryption or production configuration is unsafe.
    configure_logging()
    yield


app = FastAPI(
    title="HookDesk",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    description="Durable courier webhook inbox. Cookie-authenticated writes require X-CSRF-Token.",
    responses={
        code: {"model": ErrorResponse}
        for code in (400, 401, 403, 404, 409, 413, 415, 422, 429, 503)
    },
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(routes.router)
app.include_router(webhooks.router)
app.include_router(health.router)
app.include_router(health.router, prefix="/api/v1")
app.include_router(web_router)


def openapi_schema():
    if app.openapi_schema is None:
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        courier = CourierEvent.model_json_schema(ref_template="#/components/schemas/{model}")
        schema["components"]["schemas"].update(courier.pop("$defs", {}))
        schema["components"]["schemas"]["CourierEvent"] = courier
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = openapi_schema


@app.get("/docs", include_in_schema=False)
def api_docs(request: Request):
    return templates.TemplateResponse(request, "api_docs.html", {})


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex
    token = request_id_var.set(request.state.request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        # Bootstrap requires inline style attributes; scripts remain local and external.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        if get_settings().cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        logging.getLogger(__name__).info(
            "http.request",
            extra={
                "route": getattr(request.scope.get("route"), "path", "unmatched"),
                "method": request.method,
                "status": response.status_code,
            },
        )
        return response
    finally:
        request_id_var.reset(token)


def error_response(request, status, code, message):
    request_id = getattr(request.state, "request_id", "-")
    data = {"code": code, "message": message, "request_id": request_id}
    if request.url.path.startswith(("/api/", "/webhooks/", "/health/")):
        return JSONResponse(status_code=status, content=data)
    if status == 401 and request.method == "GET":
        response = RedirectResponse("/login", status_code=303)
        if request.headers.get("HX-Request"):
            response.headers["HX-Redirect"] = "/login"
        return response
    template = "partials/error.html" if request.headers.get("HX-Request") else "error.html"
    return templates.TemplateResponse(request, template, data, status_code=status)


@app.exception_handler(AppError)
async def app_error(request, exc):
    return error_response(request, exc.status, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
@app.exception_handler(ValidationError)
async def validation_error(request, exc):
    errors = exc.errors(include_input=False) if isinstance(exc, ValidationError) else exc.errors()
    message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors)
    return error_response(request, 422, "validation_error", message[:1000])


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    logging.getLogger(__name__).error("database.unavailable")
    return error_response(
        request, 503, "database_unavailable", "Database operation failed; try again"
    )


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return error_response(request, exc.status_code, "http_error", str(exc.detail))


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    logging.getLogger(__name__).error("http.internal_error")
    return error_response(
        request, 500, "internal_error", "Unexpected server error; contact an administrator"
    )
