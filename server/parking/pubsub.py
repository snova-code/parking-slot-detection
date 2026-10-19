# server/parking/pubsub.py
"""Redis pub/sub for live slot updates.

Redis is a bus here, not a database. A batch is durable in Postgres before
anything is published, so a publish failure costs live push and nothing else —
which is what 02_ARCHITECTURE.md promises. Subscribers recover by reconnecting
and re-snapshotting; pub/sub keeps no history and none is wanted.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from functools import lru_cache
from uuid import UUID

import redis
from django.conf import settings

logger = logging.getLogger(__name__)


def channel_for_lot(lot_id: UUID | str) -> str:
    return f"lot:{lot_id}:updates"


@lru_cache(maxsize=1)
def client() -> redis.Redis:
    """One connection pool per process. redis-py pools are thread-safe; each
    subscriber borrows a connection only while subscribed."""
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_keepalive=True,
        health_check_interval=30,
    )


def publish(lot_id, message: dict) -> bool:
    """Never raises. Losing an update is survivable; losing the request that
    stored the events is not."""
    try:
        client().publish(channel_for_lot(lot_id), json.dumps(message, default=str))
        return True
    except redis.RedisError:
        logger.warning("live update not published lot=%s", lot_id, exc_info=True)
        return False


@contextmanager
def subscription(lot_id):
    pubsub = client().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(channel_for_lot(lot_id))
    try:
        yield pubsub
    finally:
        try:
            pubsub.close()
        except redis.RedisError:
            logger.warning("could not close subscription lot=%s", lot_id, exc_info=True)