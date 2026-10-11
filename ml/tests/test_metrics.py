"""Hand-computed confusion matrices. Metric code is where silent sign errors
live, so it is tested against arithmetic done on paper, not against real data."""
from __future__ import annotations

import math

import pytest

from apms_ml.metrics import Confusion


def approx(value, expected):
    assert value is not None
    assert math.isclose(value, expected, rel_tol=1e-9)


def test_worked_example():
    # 100 slots: 60 occupied, 40 vacant.
    # Called occupied: 54 correctly, 8 vacant slots wrongly.
    c = Confusion(tp=54, fn=6, fp=8, tn=32)
    assert c.n == 100
    approx(c.accuracy, 0.86)
    approx(c.precision, 54 / 62)
    approx(c.recall, 0.90)
    approx(c.specificity, 32 / 40)
    approx(c.f1, 2 * (54 / 62) * 0.9 / ((54 / 62) + 0.9))
    approx(c.occupied_share, 0.60)
    # 38 slots reported vacant, 6 of them actually occupied
    approx(c.false_vacancy_rate, 6 / 38)
    approx(c.miss_rate, 6 / 60)
    approx(c.balanced_accuracy, (0.9 + 0.8) / 2)


def test_two_error_rates_diverge_on_a_full_lot():
    """96 of 100 occupied. Both rates come from the same 4 misses."""
    c = Confusion(tp=92, fn=4, fp=0, tn=4)
    approx(c.miss_rate, 4 / 96)          # 0.042 — looks fine
    approx(c.false_vacancy_rate, 4 / 8)  # 0.500 — half the advice is wrong
    assert c.false_vacancy_rate > 10 * c.miss_rate


def test_always_occupied_predictor():
    c = Confusion(tp=60, fn=0, fp=40, tn=0)
    approx(c.recall, 1.0)
    approx(c.accuracy, 0.6)
    assert c.false_vacancy_rate is None   # nothing reported vacant
    approx(c.miss_rate, 0.0)
    approx(c.specificity, 0.0)


def test_always_vacant_predictor():
    c = Confusion(tp=0, fn=60, fp=0, tn=40)
    assert c.precision is None            # nothing reported occupied
    assert c.f1 is None
    approx(c.false_vacancy_rate, 60 / 100)
    approx(c.miss_rate, 1.0)


def test_empty_confusion_yields_none_not_zero():
    c = Confusion()
    for key in ("accuracy", "precision", "recall", "f1",
                "false_vacancy_rate", "miss_rate", "balanced_accuracy"):
        assert getattr(c, key) is None, key


def test_update_assigns_each_cell_correctly():
    c = Confusion()
    c.update(truth=1, prediction=1)
    c.update(truth=1, prediction=0)
    c.update(truth=0, prediction=1)
    c.update(truth=0, prediction=0)
    assert (c.tp, c.fn, c.fp, c.tn) == (1, 1, 1, 1)


def test_update_many_rejects_length_mismatch():
    with pytest.raises(ValueError):
        Confusion().update_many([1, 0, 1], [1, 0])