# server/parking/security.py
"""Worker authentication.

A device token identifies exactly one camera and, through it, one
organization. The lookup is necessarily unscoped — the token is what decides
the tenant — which is the case `Camera.unscoped` exists for.

Human endpoints never list this class and worker endpoints never list the
session classes, so neither credential can open the other's routes. The
separation is structural, not a runtime check.

ninja's HttpBearer logs the raw Authorization header when DEBUG is on and the
scheme is not "Bearer". DEBUG is off wherever real tokens exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ninja.security.http import HttpBearer

from parking.models import Camera
from parking.tokens import hash_device_token, is_well_formed


@dataclass(frozen=True)
class DeviceContext:
    camera: Camera

    @property
    def org_id(self) -> UUID:
        return self.camera.org_id


class DeviceTokenAuth(HttpBearer):
    def authenticate(self, request, token):
        if not is_well_formed(token):
            return None
        camera = (Camera.unscoped.select_related("lot")
                  .filter(device_token_hash=hash_device_token(token),
                          is_active=True, lot__is_active=True)
                  .first())
        return DeviceContext(camera=camera) if camera else None


device_token = DeviceTokenAuth()