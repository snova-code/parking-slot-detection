# server/accounts/api.py
"""Session authentication for humans.

Browser protocol:
  1. GET  /auth/csrf           sets the csrftoken cookie
  2. POST /auth/login          header X-CSRFToken: <cookie value>
  3. later unsafe requests     X-CSRFToken with the cookie's CURRENT value —
                               login rotates it
"""
from __future__ import annotations

from uuid import UUID

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from ninja import Router, Schema
from pydantic import field_validator
from config.throttles import login_throttle

from accounts.models import Membership
from accounts.phone import normalize_phone
from accounts.security import ACTIVE_ORG_SESSION_KEY, enforce_csrf, login_required
from config.errors import ApiError

router = Router(tags=["auth"])


class CsrfOut(Schema):
    csrf_token: str


class LoginIn(Schema):
    phone_number: str
    password: str

    @field_validator("phone_number")
    @classmethod
    def _to_e164(cls, value: str) -> str:
        return normalize_phone(value)


class MembershipOut(Schema):
    org_id: UUID
    org_slug: str
    org_name: str
    role: str


class MeOut(Schema):
    user_id: UUID
    phone_number: str
    full_name: str
    active_org_id: UUID | None
    memberships: list[MembershipOut]


class ActiveOrgIn(Schema):
    org_id: UUID


def _me(request, user) -> dict:
    # Membership.unscoped: the caller's OWN memberships across organizations —
    # scoped by user, deliberately not by org.
    memberships = (Membership.unscoped.select_related("org")
                   .filter(user=user).order_by("org__name"))
    return {
        "user_id": user.pk,
        "phone_number": user.phone_number,
        "full_name": user.full_name,
        "active_org_id": request.session.get(ACTIVE_ORG_SESSION_KEY),
        "memberships": [
            {"org_id": m.org_id, "org_slug": m.org.slug,
             "org_name": m.org.name, "role": m.role}
            for m in memberships
        ],
    }


@router.get("/csrf", response=CsrfOut, auth=None)
def csrf(request):
    return {"csrf_token": get_token(request)}


@router.post("/login", response=MeOut, auth=None, throttle=[login_throttle])
def login_view(request, payload: LoginIn):
    # Unauthenticated, so session auth's CSRF check never runs here. Login CSRF
    # would sign the victim's browser into the attacker's account.
    enforce_csrf(request)
    user = authenticate(request, phone_number=payload.phone_number,
                        password=payload.password)
    if user is None:
        # One response for unknown phone and wrong password. Django's backend
        # also hashes a dummy password for unknown users, so timing matches.
        raise ApiError(401, "invalid_credentials",
                       "Phone number or password is incorrect.")
    login(request, user)   # new session key; rotated CSRF token
    return _me(request, user)


@router.post("/logout", response={204: None}, auth=login_required)
def logout_view(request):
    logout(request)
    return 204, None


@router.get("/me", response=MeOut, auth=login_required)
def me(request):
    return _me(request, request.auth)


@router.put("/active-org", response=MeOut, auth=login_required)
def set_active_org(request, payload: ActiveOrgIn):
    membership = (Membership.objects.for_org(payload.org_id)
                  .filter(user=request.auth).first())
    if membership is None:
        # 404, not 403: a non-member learns nothing about whether it exists.
        raise ApiError(404, "org_not_found", "No such organization.")
    request.session[ACTIVE_ORG_SESSION_KEY] = str(membership.org_id)
    return _me(request, request.auth)