"""Error envelope from docs/api-specification.md § Errors."""

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()


class AppError(Exception):
    code = "internal"
    http_status = 500
    message = "Internal error."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message


class Unauthorized(AppError):
    code, http_status, message = "unauthorized", 401, "Unauthorized."


class SignatureInvalid(AppError):
    code, http_status, message = "signature_invalid", 403, "Invalid signature."


class NotFound(AppError):
    code, http_status, message = "not_found", 404, "Resource not found."
    retry = False  # jobs: a deleted row never comes back


class LinkExpired(AppError):
    code, http_status, message = "link_expired", 410, "Link expired."


class ValidationFailed(AppError):
    code, http_status, message = "validation_error", 422, "Validation failed."
    retry = False  # jobs: the same input fails the same way


class IRInvalid(AppError):
    """Recorded as a job error_code only; never returned by a route."""

    code, http_status, message = "ir_invalid", 422, "The template IR failed validation twice."
    retry = False  # already retried once with the errors fed back


class Conflict(AppError):
    code, http_status, message = "conflict", 409, "Conflict."


class PayloadTooLarge(AppError):
    code, http_status, message = "payload_too_large", 413, "Upload exceeds the size limit."


class UnsupportedMediaType(AppError):
    code, http_status, message = "unsupported_media_type", 415, "Unsupported file type."


class UpstreamUnavailable(AppError):
    code, http_status, message = "upstream_unavailable", 503, "An upstream service is unavailable."


def _request_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("request_id")


def error_response(status: int, code: str, message: str) -> JSONResponse:
    rid = _request_id()
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": rid}},
        headers={"X-Request-Id": rid} if rid else None,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.http_status >= 500:
            # Type name only: a cause's message may carry document text or a keyed URL.
            cause = type(exc.__cause__).__name__ if exc.__cause__ else None
            log.warning("app_error", code=exc.code, cause=cause)
        return error_response(exc.http_status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only field locations, never submitted values (they may contain document text).
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        return error_response(422, "validation_error", f"Invalid fields: {fields}")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return error_response(404, "not_found", NotFound.message)
        if exc.status_code == 405:
            return error_response(405, "method_not_allowed", "Method not allowed.")
        return error_response(exc.status_code, "internal", "Request failed.")
