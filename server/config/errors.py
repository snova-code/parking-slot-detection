# server/config/errors.py
"""The uniform error shape from 04_API_CONTRACT.md, applied to every error the
API can produce, including ninja's own:

    {"error": {"code": "...", "message": "...", "detail": {...}}}

Validation errors are reduced to loc/msg/type. pydantic places the exception a
validator raised inside each error's `ctx`, and that object is not JSON-
serialisable — passed through unfiltered, the first validator that raises turns
a 422 into a 500.
"""
from __future__ import annotations

from django.http import Http404
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError

STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    429: "rate_limited",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 detail: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}


def error_body(code: str, message: str, detail: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def install_error_handlers(api: NinjaAPI) -> None:
    def api_error(request, exc: ApiError):
        return api.create_response(
            request, error_body(exc.code, exc.message, exc.detail), status=exc.status)

    def validation_error(request, exc: ValidationError):
        errors = [
            {"loc": list(item.get("loc", ())),
             "msg": item.get("msg", ""),
             "type": item.get("type", "")}
            for item in exc.errors
        ]
        return api.create_response(
            request,
            error_body("validation_failed", "Request did not match the schema.",
                       {"errors": errors}),
            status=422,
        )

    def http_error(request, exc: HttpError):
        code = STATUS_CODES.get(exc.status_code, "http_error")
        return api.create_response(request, error_body(code, str(exc)),
                                   status=exc.status_code)

    def not_found(request, exc: Http404):
        return api.create_response(request, error_body("not_found", "Not found."),
                                   status=404)

    api.add_exception_handler(ApiError, api_error)
    api.add_exception_handler(ValidationError, validation_error)
    api.add_exception_handler(HttpError, http_error)
    api.add_exception_handler(Http404, not_found)