# worker/tests/test_smoothing.py
"""The vote, and the committed state that decides what gets sent."""
from __future__ import annotations

import pytest

from worker.contract import SlotSpec
from worker.smoothing import OCCUPIED, UNKNOWN, VACANT, SlotVoter, VoterSet, window_seconds

SQUARE = [[100, 100], [200, 100], [200, 180], [100, 180]]


def slot(index: int) -> SlotSpec:
    return SlotSpec(id=f"a91c0000-0000-4000-8000-00000000000{index}",
                    label=f"A-0{index}", polygon=SQUARE)


def voter(window=9, min_agreement=6) -> SlotVoter:
    return SlotVoter(slot_id=slot(1).id, label="A-01",
                     window=window, min_agreement=min_agreement)


def feed(subject: SlotVoter, probabilities) -> list[str]:
    return [status for status in (subject.observe(p) for p in probabilities)
            if status is not None]


def test_the_first_observation_already_decides():
    """Warm-up scales the requirement, so a fresh worker does not wait a whole
    window before reporting anything."""
    assert feed(voter(), [0.9]) == [OCCUPIED]


def test_a_single_stray_frame_does_not_flip_a_settled_slot():
    subject = voter(window=9, min_agreement=6)
    feed(subject, [0.1] * 9)
    assert subject.committed == VACANT
    assert feed(subject, [0.99]) == []
    assert subject.committed == VACANT


def test_a_sustained_change_flips_it():
    subject = voter(window=9, min_agreement=6)
    feed(subject, [0.1] * 9)
    changes = feed(subject, [0.99] * 6)
    assert changes == [OCCUPIED]


def test_m_of_1_is_the_raw_classifier():
    subject = voter(window=1, min_agreement=1)
    assert feed(subject, [0.9, 0.1, 0.9]) == [OCCUPIED, VACANT, OCCUPIED]


def test_only_transitions_are_reported():
    subject = voter(window=3, min_agreement=2)
    assert len(feed(subject, [0.9] * 50)) == 1


def test_a_resync_re_announces_without_replaying_the_window():
    subject = voter(window=9, min_agreement=6)
    feed(subject, [0.9] * 9)
    subject.forget_committed()
    assert subject.committed == UNKNOWN
    # One frame is enough: the history survived, so the verdict is immediate.
    assert feed(subject, [0.9]) == [OCCUPIED]


def test_window_duration_is_computable():
    """M=9 was 45 minutes on PKLot's 5-minute frames and is 4.5s at 2 fps."""
    assert window_seconds(9, 2.0) == 4.5
    assert window_seconds(9, 1 / 300) == 2700


def test_config_change_keeps_state_for_surviving_slots():
    first = VoterSet([slot(1), slot(2)], window=3, min_agreement=2)
    first.observe([slot(1).id, slot(2).id], [0.9, 0.9])
    first.observe([slot(1).id, slot(2).id], [0.9, 0.9])

    second = VoterSet([slot(1), slot(3)], window=3, min_agreement=2)
    second.carry_over(first)
    assert second.voters[slot(1).id].committed == OCCUPIED
    assert second.voters[slot(3).id].committed == UNKNOWN
    # History is not carried: a re-annotated polygon sees different pixels.
    assert len(second.voters[slot(1).id].history) == 0


def test_voterset_reports_the_previous_status_with_each_change():
    voters = VoterSet([slot(1)], window=1, min_agreement=1)
    [(subject, previous, probability)] = voters.observe([slot(1).id], [0.9])
    assert (previous, subject.committed) == (UNKNOWN, OCCUPIED)
    assert probability == pytest.approx(0.9)