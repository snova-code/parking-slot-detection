# server/accounts/security.py
"""Human authentication and organization resolution — the request-side half of
the scoping layer. (03_DATA_MODEL.md calls this "a mixin that resolves the org
from the request"; with ninja it is an auth class that runs before the handler.)

Two classes, both reading Django's session cookie:

  LoginRequired     a logged-in user. For /auth/* endpoints.
  OrgMember(roles)  a logged-in user, a resolved active organization, and a
                    membership there whose role is in `roles`. The handler
                    receives an OrgContext as request.auth.

The session stores only the active organization's id. Role is read from
Membership on every request, so a removal or demotion takes effect on the
member's next request.

CSRF is enforced after a session is confirmed — so anonymous requests get 401,
not 403 — through the same function the login endpoint calls. `self.csrf`
stays True because it truthfully describes this auth, and ninja's docs page
reads it to decide whether to send the token.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.conf import settings
from ninja.security.apikey import APIKeyCookie
from ninja.utils import check_csrf

from accounts.models import Membership, Organization, Role, User
from config.errors import ApiError

ACTIVE_ORG_SESSION_KEY = "active_org_id"


def enforce_csrf(request) -> None:
    if check_csrf(request) is not None:
        raise ApiError(
            403, "csrf_failed",
            "CSRF token missing or incorrect. GET /api/v1/auth/csrf and send the "
            "csrftoken cookie's current value as X-CSRFToken.",
        )


@dataclass(frozen=True)
class OrgContext:
    user: User
    org: Organization
    membership: Membership

    @property
    def role(self) -> Role:
        return Role(self.membership.role)


class _SessionAuth(APIKeyCookie):
    param_name = settings.SESSION_COOKIE_NAME

    def _get_key(self, request):
        return request.COOKIES.get(self.param_name)

    def _authenticated_user(self, request, key):
        if not key or not request.user.is_authenticated:
            return None
        enforce_csrf(request)
        return request.user


class LoginRequired(_SessionAuth):
    def authenticate(self, request, key):
        return self._authenticated_user(request, key)


class OrgMember(_SessionAuth):
    def __init__(self, roles) -> None:
        roles = frozenset(roles)
        if not roles or not all(isinstance(role, Role) for role in roles):
            raise TypeError("OrgMember needs a non-empty set of Role members, not strings")
        self.roles = roles
        super().__init__()

    def authenticate(self, request, key):
        user = self._authenticated_user(request, key)
        if user is None:
            return None
        membership = resolve_membership(request, user)
        if Role(membership.role) not in self.roles:
            # 403, not 404: this member can already see the resource; what they
            # lack is permission to change it.
            raise ApiError(
                403, "insufficient_role",
                "Your role in this organization does not allow this action.",
                {"required": sorted(role.value for role in self.roles),
                 "held": membership.role},
            )
        return OrgContext(user=user, org=membership.org, membership=membership)


def resolve_membership(request, user) -> Membership:
    active = request.session.get(ACTIVE_ORG_SESSION_KEY)
    if active:
        membership = (Membership.objects.for_org(uuid.UUID(active))
                      .select_related("org").filter(user=user).first())
        if membership is not None:
            return membership
        # Revoked, or the organization was deleted, since it was selected.
        del request.session[ACTIVE_ORG_SESSION_KEY]

    # Membership.unscoped: the user's OWN memberships across organizations,
    # read only to decide whether a default choice is unambiguous.
    candidates = list(Membership.unscoped.select_related("org")
                      .filter(user=user).order_by("created_at")[:2])
    if len(candidates) == 1:
        request.session[ACTIVE_ORG_SESSION_KEY] = str(candidates[0].org_id)
        return candidates[0]
    if not candidates:
        raise ApiError(403, "no_membership",
                       "This account does not belong to any organization.")
    raise ApiError(
        403, "org_selection_required",
        "This account belongs to several organizations. "
        "Choose one with PUT /api/v1/auth/active-org.",
    )


login_required = LoginRequired()
any_member = OrgMember({Role.OWNER, Role.OPERATOR})
owner_only = OrgMember({Role.OWNER})