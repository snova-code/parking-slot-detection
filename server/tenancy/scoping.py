# server/tenancy/scoping.py
"""Tenant scoping for every org-owned model.

`Model.objects.get_queryset()` raises. The only entry point is
`Model.objects.for_org(org)`, which returns a queryset already filtered to that
organization — a view cannot forget the filter, because no unfiltered queryset
exists to forget it on. v1's unauthenticated ajax endpoint returning every
tenant's data is the failure this makes unrepresentable.

`Model.unscoped` is the deliberate escape hatch for genuinely cross-tenant work:
the stale-heartbeat sweeper, test setup, one-off maintenance. Grep for it.

Consequences, all deliberate:
  - Reverse relation managers (`lot.cameras.all()`) raise, because Django builds
    them from the default manager's class. Query the child model instead:
    `Camera.objects.for_org(org).filter(lot=lot)`.
  - `Model.full_clean()` raises: its uniqueness checks use the default manager.
    Input validation lives in the API schemas; database constraints are the
    backstop.
  - Forward FK access (`camera.lot`), reverse one-to-one (`slot.state`),
    `save`, `delete`, `refresh_from_db` and ORM cascades all use Django's base
    manager and are unaffected.
"""
from __future__ import annotations

import uuid

from django.db import models


class UnscopedQueryError(RuntimeError):
    """Tenant-owned data was queried without an organization."""


def _org_id(org) -> uuid.UUID:
    if isinstance(org, uuid.UUID):
        return org
    pk = getattr(org, "pk", None)
    if isinstance(pk, uuid.UUID):
        return pk
    raise TypeError(
        f"for_org() needs an Organization or its UUID, got {org!r}. "
        "None here usually means the request's organization was never resolved."
    )


class OrgQuerySet(models.QuerySet):
    """A queryset bound to one organization. Built only by `for_org`."""

    _org_id: uuid.UUID | None = None

    def _clone(self):
        clone = super()._clone()
        clone._org_id = self._org_id
        return clone

    def _bound_org(self) -> uuid.UUID:
        if self._org_id is None:
            raise UnscopedQueryError("OrgQuerySet used without an organization")
        return self._org_id

    def _claim(self, candidate: uuid.UUID | None, what: str) -> uuid.UUID:
        bound = self._bound_org()
        if candidate is not None and candidate != bound:
            raise ValueError(
                f"{what} belongs to organization {candidate}, but this queryset "
                f"is scoped to {bound}"
            )
        return bound

    def create(self, **kwargs):
        given = kwargs.pop("org", None)
        given_id = kwargs.pop("org_id", None)
        if given is not None:
            given_id = _org_id(given)
        kwargs["org_id"] = self._claim(given_id, self.model.__name__)
        return super().create(**kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        for obj in objs:
            obj.org_id = self._claim(obj.org_id, f"{self.model.__name__} {obj.pk}")
        return super().bulk_create(objs, *args, **kwargs)

    def update(self, **kwargs):
        if "org" in kwargs or "org_id" in kwargs:
            raise ValueError("Moving rows between organizations is not supported.")
        return super().update(**kwargs)


class OrgScopedManager(models.Manager):
    def get_queryset(self):
        name = self.model.__name__
        raise UnscopedQueryError(
            f"{name}.objects needs an organization. Use "
            f"{name}.objects.for_org(org), or {name}.unscoped for deliberate "
            "cross-tenant work."
        )

    def for_org(self, org) -> OrgQuerySet:
        org_id = _org_id(org)
        queryset = OrgQuerySet(model=self.model, using=self._db, hints=self._hints)
        queryset._org_id = org_id
        return queryset.filter(org_id=org_id)


class TenantModel(models.Model):
    """Base for every tenant-owned table: non-null org FK, scoped default
    manager, explicit unscoped escape hatch."""

    org = models.ForeignKey("accounts.Organization", on_delete=models.CASCADE)

    objects = OrgScopedManager()
    unscoped = models.Manager()

    class Meta:
        abstract = True