# server/tests/test_org_scoping.py
"""The scoping manager: there is no unscoped queryset to forget a filter on."""
from __future__ import annotations

import pytest
from django.apps import apps
from django.db import models

from accounts.models import Organization, User
from parking.models import Camera, ParkingLot
from tenancy.scoping import OrgQuerySet, OrgScopedManager, UnscopedQueryError
from tests.factories import camera_fields

TENANT_EXEMPT = {Organization, User}
TENANT_MODELS = [
    model
    for label in ("accounts", "parking")
    for model in apps.get_app_config(label).get_models()
    if model not in TENANT_EXEMPT
]


# ---------------------------------------------------------- structure ----
def test_tenant_model_set_is_exactly_what_the_data_model_lists():
    """Adding a model must be a conscious act: update this set, and the
    parametrised test below then holds the new model to the same rules."""
    assert {m.__name__ for m in TENANT_MODELS} == {
        "Membership", "ParkingLot", "Camera", "ParkingSlot",
        "SlotState", "OccupancyEvent",
    }


@pytest.mark.parametrize("model", TENANT_MODELS, ids=lambda m: m.__name__)
def test_every_tenant_model_is_scoped(model):
    org_field = model._meta.get_field("org")
    assert org_field.related_model is Organization
    assert org_field.null is False
    assert isinstance(model._default_manager, OrgScopedManager)
    assert type(model.unscoped) is models.Manager


# -------------------------------------------------- unscoped refusal ----
@pytest.mark.parametrize("call", [
    lambda: list(Camera.objects.all()),
    lambda: Camera.objects.filter(name="cam"),
    lambda: Camera.objects.count(),
    lambda: Camera.objects.exists(),
    lambda: Camera.objects.get(name="cam"),
    lambda: Camera.objects.first(),
    lambda: Camera.objects.create(name="cam"),
], ids=["all", "filter", "count", "exists", "get", "first", "create"])
def test_unscoped_access_raises(db, call):
    with pytest.raises(UnscopedQueryError):
        call()


@pytest.mark.parametrize("bad", [None, "alpha", 42])
def test_for_org_rejects_anything_but_an_org_or_its_uuid(db, bad):
    with pytest.raises(TypeError):
        Camera.objects.for_org(bad)


# ---------------------------------------------------------- isolation ----
def test_for_org_returns_only_that_orgs_rows(make_tree):
    a, b = make_tree("alpha"), make_tree("beta")
    assert list(Camera.objects.for_org(a.org)) == [a.camera]
    assert list(Camera.objects.for_org(b.org)) == [b.camera]


def test_another_orgs_row_is_not_found_rather_than_forbidden(make_tree):
    """The data-layer basis for returning 404, not 403, on cross-org access."""
    a, b = make_tree("alpha"), make_tree("beta")
    with pytest.raises(Camera.DoesNotExist):
        Camera.objects.for_org(a.org).get(pk=b.camera.pk)


def test_for_org_accepts_the_org_uuid(make_tree):
    a = make_tree("alpha")
    assert list(Camera.objects.for_org(a.org.pk)) == [a.camera]


def test_scope_survives_chaining(make_tree):
    a = make_tree("alpha")
    chained = (Camera.objects.for_org(a.org)
               .filter(is_active=True).exclude(name="nope").order_by("name"))
    assert isinstance(chained, OrgQuerySet)
    created = chained.create(**camera_fields(a.lot, name="second"))
    assert created.org_id == a.org.pk


# ------------------------------------------------------------- writes ----
def test_create_refuses_a_conflicting_org(make_tree):
    a, b = make_tree("alpha"), make_tree("beta")
    with pytest.raises(ValueError):
        Camera.objects.for_org(a.org).create(org=b.org, **camera_fields(a.lot, name="x"))


def test_bulk_create_fills_in_and_guards_org(make_tree):
    a, b = make_tree("alpha"), make_tree("beta")
    scoped = Camera.objects.for_org(a.org)
    [made] = scoped.bulk_create([Camera(**camera_fields(a.lot, name="bulk"))])
    assert made.org_id == a.org.pk
    with pytest.raises(ValueError):
        scoped.bulk_create([Camera(org=b.org, **camera_fields(a.lot, name="bad"))])


def test_update_cannot_move_rows_between_orgs(make_tree):
    a, b = make_tree("alpha"), make_tree("beta")
    with pytest.raises(ValueError):
        ParkingLot.objects.for_org(a.org).update(org=b.org)


# ---------------------------------------------------------- relations ----
def test_forward_relations_still_work(make_tree):
    a = make_tree("alpha")
    camera = Camera.objects.for_org(a.org).get(pk=a.camera.pk)
    assert camera.lot == a.lot


def test_reverse_relation_managers_raise_by_design(make_tree):
    """Documented cost of the design. If this starts passing without raising,
    someone changed the default manager — find out why before accepting it."""
    a = make_tree("alpha")
    with pytest.raises(UnscopedQueryError):
        list(a.lot.cameras.all())