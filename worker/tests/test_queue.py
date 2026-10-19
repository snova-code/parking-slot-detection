# worker/tests/test_queue.py
"""The outbox: the file that either keeps 02_ARCHITECTURE.md's promise or not."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from worker.queue import EventQueue

SLOT = "a91c0000-0000-4000-8000-000000000001"


def moment(minute: int) -> datetime:
    return datetime(2026, 9, 10, 10, minute, tzinfo=timezone.utc)


def add(queue: EventQueue, minute: int, status: str = "OCCUPIED") -> dict:
    return queue.append(slot_id=SLOT, previous_status="VACANT", status=status,
                        confidence=0.9412, observed_at=moment(minute),
                        model_version="test-model")


def test_events_get_an_id_before_any_send_is_attempted(tmp_path):
    queue = EventQueue(tmp_path / "q.sqlite3")
    payload = add(queue, 1)
    assert payload["id"] and payload["confidence"] == 0.941
    assert payload["observed_at"] == "2026-09-10T10:01:00+00:00"


def test_the_queue_survives_the_process(tmp_path):
    path = tmp_path / "q.sqlite3"
    queue = EventQueue(path)
    add(queue, 1)
    add(queue, 2)
    queue.close()

    recovered = EventQueue(path)
    assert recovered.depth() == 2
    assert [event.payload["observed_at"] for event in recovered.peek(10)] == [
        moment(1).isoformat(), moment(2).isoformat()]


def test_acknowledging_removes_only_what_was_sent(tmp_path):
    queue = EventQueue(tmp_path / "q.sqlite3")
    for minute in range(5):
        add(queue, minute)
    batch = queue.peek(3)
    queue.acknowledge(batch)
    assert queue.depth() == 2
    assert queue.peek(10)[0].payload["observed_at"] == moment(3).isoformat()


def test_nothing_is_lost_when_a_send_is_not_acknowledged(tmp_path):
    queue = EventQueue(tmp_path / "q.sqlite3")
    add(queue, 1)
    first = queue.peek(10)
    second = queue.peek(10)          # a timeout: send attempted, not acknowledged
    assert [event.id for event in first] == [event.id for event in second]
    assert queue.depth() == 1


def test_a_full_queue_drops_the_oldest_and_counts_it(tmp_path, caplog):
    queue = EventQueue(tmp_path / "q.sqlite3", max_events=3)
    with caplog.at_level("ERROR"):
        for minute in range(5):
            add(queue, minute)
    assert queue.depth() == 3
    assert queue.dropped == 2
    assert [event.payload["observed_at"] for event in queue.peek(10)] == [
        moment(2).isoformat(), moment(3).isoformat(), moment(4).isoformat()]
    assert "queue full" in caplog.text