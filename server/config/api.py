# server/config/api.py
"""The single NinjaAPI instance, mounted at /api/v1/.

Schema at /api/v1/openapi.json, interactive docs at /api/v1/docs.

Default auth is `any_member`: an endpoint that does not declare its auth
requires a logged-in member of the active organization. Public endpoints must
say `auth=None`; the worker router overrides with the device-token class.
Forgetting to declare auth fails closed.
"""
from ninja import NinjaAPI

from accounts.api import router as auth_router
from accounts.security import any_member
from config.errors import install_error_handlers
from parking.worker_api import worker_router
from parking.api import cameras_router, lots_router

api = NinjaAPI(
    title="APMS v2",
    version="1",
    urls_namespace="api-v1",
    description="Multi-tenant parking occupancy API. Contract: docs/04_API_CONTRACT.md.",
    auth=any_member,
)
install_error_handlers(api)

api.add_router("/auth", auth_router)
api.add_router("/cameras", cameras_router)
api.add_router("/worker", worker_router)
api.add_router("/lots", lots_router)