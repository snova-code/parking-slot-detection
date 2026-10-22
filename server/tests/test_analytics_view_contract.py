# server/tests/test_analytics_view_contract.py
"""What the analytics page depends on.

The charts are checked by eye; what is pinned here is the contract they read.
The coverage field in particular: the page renders a bucket as unreadable
below a threshold, and that only works if coverage is always present and
always comparable to the rate beside it.
"""
from __future__ import annotations

from datetime import timedelta

from parking.models import CameraOutage
from tests.factories import make_event
from tests.test_analytics import api as analytics_api    # noqa: F401  (fixture)
from tests.test_analytics import fetch, ktm, outage


def test_every_bucket_carries_rate_and_coverage(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 12))
    for bucket in body["occupancy_rate"]:
        assert set(bucket) == {"bucket", "rate", "coverage"}
        assert bucket["coverage"] is None or 0.0 <= bucket["coverage"] <= 1.0
        assert bucket["rate"] is None or 0.0 <= bucket["rate"] <= 1.0


def test_a_bucket_with_no_coverage_reports_no_rate(make_tree, api):
    """The page hatches these out. If coverage were 0 while rate held a
    number, it would draw a confident bar over no evidence."""
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    body = fetch(api(a), a.lot, ktm(10, 8), ktm(10, 11))
    first = body["occupancy_rate"][0]
    assert (first["coverage"], first["rate"]) == (0.0, None)


def test_partial_coverage_lands_between_the_extremes(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    outage(a.camera, ktm(10, 10, 30), ktm(10, 11))
    body = fetch(api(a), a.lot, ktm(10, 10), ktm(10, 11))
    assert body["occupancy_rate"][0]["coverage"] == 0.5


def test_the_summary_fields_the_page_prints_are_present(make_tree, api):
    a = make_tree("alpha")
    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 12))
    assert set(body) >= {"lot_id", "timezone", "bucket", "slots_counted",
                         "observed_slot_days", "occupancy_rate",
                         "dwell_minutes", "turnover_per_slot_per_day"}
    assert set(body["dwell_minutes"]) == {"p50", "p90", "mean", "samples"}


def test_an_empty_range_still_renders(make_tree, api):
    """A lot with no events must produce a drawable response, not an error."""
    a = make_tree("alpha")
    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 12))
    assert len(body["occupancy_rate"]) == 3
    assert body["dwell_minutes"]["samples"] == 0
    assert body["turnover_per_slot_per_day"] is None
    assert body["observed_slot_days"] == 0.0