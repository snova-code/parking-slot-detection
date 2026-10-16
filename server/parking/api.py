# server/parking/api.py
"""Camera endpoints for humans."""
from __future__ import annotations

from uuid import UUID

from django.http import HttpResponse
from ninja import Router, Schema

from accounts.security import owner_only
from config.errors import ApiError
from parking.models import Camera
from parking.tokens import generate_device_token

cameras_router = Router(tags=["cameras"])


class RotatedTokenOut(Schema):
    camera_id: UUID
    device_token: str


@cameras_router.post("/{camera_id}/token:rotate", response=RotatedTokenOut,
                     auth=owner_only)
def rotate_device_token(request, camera_id: UUID, response: HttpResponse):
    """Replace a camera's device token. The old token stops working at once —
    no overlap window, because rotation is usually a response to a leak. The
    new token appears in this response only."""
    token, digest = generate_device_token()
    updated = (Camera.objects.for_org(request.auth.org)
               .filter(pk=camera_id).update(device_token_hash=digest))
    if not updated:
        # Same answer for another org's camera and a missing one.
        raise ApiError(404, "camera_not_found", "No such camera.")
    response["Cache-Control"] = "no-store"
    return {"camera_id": camera_id, "device_token": token}