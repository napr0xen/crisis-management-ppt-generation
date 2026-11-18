"""Error envelope from docs/api-specification.md § Errors."""

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


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


class LinkExpired(AppError):
    code, http_status, message = "link_expired", 410, "Link expired."


class ValidationFailed(AppError):
    code, http_status, message = "validation_error", 422, "Validation failed."


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
