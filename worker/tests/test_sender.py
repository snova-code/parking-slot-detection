# worker/tests/test_sender.py
"""Draining the outbox against a server that misbehaves."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from worker.api_client import ApiClient, AuthRejected
from worker.queue import EventQueue
from worker.sender import Sender

CAMERA = "3f2a0000-0000-4000-8000-000000000001"
SLOT = "a91c0000-0000-4000-8000-000000000001"


def queue_with(tmp_path, count: int) -> EventQueue:
    queue = EventQueue(tmp_path / "q.sqlite3")
    for index in range(count):
        queue.append(slot_id=SLOT, previous_status="VACANT", status="OCCUPIED",
                     confidence=0.9, model_version="test-model",
                     observed_at=datetime(2026, 9, 10, 10, index % 60,
                                          tzinfo=timezone.utc))
    return queue


def sender_for(queue, handler) -> Sender:
    client = ApiClient(httpx.Client(transport=httpx.MockTransport(handler),
                                    base_url="http://server/api/v1"))
    return Sender(client, queue, CAMERA)


def accepting(seen: list):
    def handler(request):
        import json
        body = json.loads(request.content)
        seen.append(body["events"])
        return httpx.Response(200, json={"accepted": len(body["events"]),
                                         "duplicates": 0, "rejected": [],
                                         "previous_status_mismatches": 0})
    return handler


def test_a_successful_flush_empties_the_queue(tmp_path):
    queue = queue_with(tmp_path, 3)
    seen = []
    assert sender_for(queue, accepting(seen)).flush() == 3
    assert queue.depth() == 0
    assert [event["slot_id"] for event in seen[0]] == [SLOT] * 3


def test_a_server_error_leaves_everything_queued_and_backs_off(tmp_path):
    queue = queue_with(tmp_path, 2)
    sender = sender_for(queue, lambda request: httpx.Response(500, text="boom"))

    assert sender.flush(now=0.0) == 0
    assert queue.depth() == 2
    assert sender.failures == 1
    assert sender.blocked_until == pytest.approx(1.0)

    # A second attempt inside the backoff window does not even try.
    assert sender.flush(now=0.5) == 0
    assert sender.failures == 1


def test_recovery_sends_everything_that_accumulated(tmp_path):
    queue = queue_with(tmp_path, 2)
    responses = [httpx.Response(503, text="down")]
    seen = []

    def handler(request):
        if responses:
            return responses.pop()
        return accepting(seen)(request)

    sender = sender_for(queue, handler)
    sender.flush(now=0.0)
    queue.append(slot_id=SLOT, previous_status="OCCUPIED", status="VACANT",
                 confidence=0.8, observed_at=datetime.now(timezone.utc),
                 model_version="test-model")

    assert sender.flush(now=99.0) == 3
    assert queue.depth() == 0
    assert sender.failures == 0


def test_rejected_events_are_dropped_not_retried(tmp_path):
    """A rejection is permanent; leaving it queued would block the queue."""
    queue = queue_with(tmp_path, 1)
    rejected_id = queue.peek(1)[0].id

    def handler(request):
        return httpx.Response(200, json={
            "accepted": 0, "duplicates": 0,
            "rejected": [{"id": rejected_id, "reason": "unknown_slot"}],
            "previous_status_mismatches": 0})

    assert sender_for(queue, handler).flush() == 1
    assert queue.depth() == 0


def test_duplicates_are_acknowledged(tmp_path):
    queue = queue_with(tmp_path, 1)
    handler = lambda request: httpx.Response(200, json={
        "accepted": 0, "duplicates": 1, "rejected": [],
        "previous_status_mismatches": 0})
    assert sender_for(queue, handler).flush() == 1
    assert queue.depth() == 0


def test_a_rejected_token_stops_the_worker(tmp_path):
    queue = queue_with(tmp_path, 1)
    handler = lambda request: httpx.Response(403, json={
        "error": {"code": "camera_mismatch", "message": "…", "detail": {}}})
    with pytest.raises(AuthRejected):
        sender_for(queue, handler).flush()
    assert queue.depth() == 1


def test_more_than_one_batch_is_sent_in_one_flush(tmp_path):
    queue = queue_with(tmp_path, 501)
    seen = []
    assert sender_for(queue, accepting(seen)).flush() == 501
    assert [len(batch) for batch in seen] == [500, 1]