# server/tests/test_api_surface.py
"""Two guards against the project's original failure: documentation that
describes something the code does not do, or does not describe something it
does.

The route inventory fails when an endpoint is added or removed without
updating docs/04_API_CONTRACT.md. The error-code inventory fails when a new
`ApiError` code appears that the contract's error table does not list. Both
are kept honest by having to edit this file, which forces the doc edit into
the same commit.
"""
from __future__ import annotations

import ast
from pathlib import Path

from django.test import Client

from config.errors import STATUS_CODES

SERVER_ROOT = Path(__file__).resolve().parent.parent

# docs/04_API_CONTRACT.md — "Endpoints"
DOCUMENTED_ROUTES = {
    ("GET", "/api/v1/auth/csrf"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/auth/me"),
    ("PUT", "/api/v1/auth/active-org"),
    ("GET", "/api/v1/lots"),
    ("POST", "/api/v1/lots"),
    ("GET", "/api/v1/lots/{lot_id}/slots"),
    ("GET", "/api/v1/lots/{lot_id}/analytics"),
    ("GET", "/api/v1/lots/{lot_id}/stream"),
    ("POST", "/api/v1/lots/{lot_id}/cameras"),
    ("POST", "/api/v1/cameras/{camera_id}/token:rotate"),
    ("POST", "/api/v1/cameras/{camera_id}/slots:bulk"),
    ("GET", "/api/v1/worker/config"),
    ("POST", "/api/v1/worker/events"),
    ("POST", "/api/v1/worker/heartbeat"),
    ("POST", "/api/v1/worker/frame"),
    ("GET", "/api/v1/cameras/{camera_id}/frame"),
}

# docs/04_API_CONTRACT.md — "Errors"
DOCUMENTED_ERROR_CODES = {
    # authentication and tenancy
    "csrf_failed", "invalid_credentials", "unauthenticated", "forbidden",
    "no_membership", "org_selection_required", "insufficient_role",
    "org_not_found", "lot_not_found", "camera_not_found", "not_found",
    # writes
    "lot_name_taken", "slots_version_conflict", "unknown_model_version",
    "invalid_polygon", "duplicate_labels",
    # worker
    "camera_mismatch", "batch_too_large", "model_unavailable",
    # reads
    "invalid_range", "range_too_large", "invalid_lot_timezone",
    # live updates and limits
    "bus_unavailable", "too_many_streams", "rate_limited",
    # generic
    "bad_request", "validation_failed", "method_not_allowed",
    "payload_too_large", "http_error",
    "invalid_frame", "frame_too_large", "frame_not_available",
}


def test_served_routes_are_the_documented_routes(db):
    schema = Client().get("/api/v1/openapi.json").json()
    served = {
        (method.upper(), path.rstrip("/") or path)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    undocumented = served - DOCUMENTED_ROUTES
    missing = DOCUMENTED_ROUTES - served
    assert not undocumented, f"served but not in the contract: {sorted(undocumented)}"
    assert not missing, f"in the contract but not served: {sorted(missing)}"


def _error_codes_in_source() -> set[str]:
    """First string argument of every ApiError(...) and error_body(...) call."""
    codes: set[str] = set()
    for path in SERVER_ROOT.rglob("*.py"):
        if "migrations" in path.parts or path.parts[-2:][0] == "tests":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in {"ApiError", "error_body"}:
                continue
            index = 1 if name == "ApiError" else 0
            if len(node.args) > index and isinstance(node.args[index], ast.Constant):
                value = node.args[index].value
                if isinstance(value, str):
                    codes.add(value)
    return codes


def test_every_error_code_is_documented():
    codes = _error_codes_in_source() | set(STATUS_CODES.values())
    undocumented = codes - DOCUMENTED_ERROR_CODES
    stale = DOCUMENTED_ERROR_CODES - codes
    assert not undocumented, f"raised but not in the contract: {sorted(undocumented)}"
    assert not stale, f"in the contract but never raised: {sorted(stale)}"