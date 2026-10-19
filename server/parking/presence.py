# server/parking/presence.py
"""How many streams one user has open (04_API_CONTRACT.md: 5).

A sorted set per user, scored by expiry. Entries are refreshed on every
heartbeat and removed on disconnect, so a process that dies without cleaning
up leaks a slot for one expiry window rather than forever.

The count and the insert are not atomic, so a burst of simultaneous connects
can exceed the cap by the size of the burst. This exists to stop abandoned
tabs from eating worker threads, not as a security control.
"""
from __future__ import annotations

import time
import uuid

from django.conf import settings

from config.errors import ApiError
from parking.pubsub import client


def _key(user_id) -> str:
    return f"sse:{user_id}"


def _ttl() -> float:
    return settings.SSE_HEARTBEAT_SECONDS * 3


def acquire(user_id) -> str:
    redis = client()
    key = _key(user_id)
    now = time.time()
    redis.zremrangebyscore(key, "-inf", now)
    if redis.zcard(key) >= settings.SSE_MAX_CONNECTIONS_PER_USER:
        raise ApiError(429, "too_many_streams",
                       "Too many open streams for this account.",
                       {"limit": settings.SSE_MAX_CONNECTIONS_PER_USER})
    token = uuid.uuid4().hex
    redis.zadd(key, {token: now + _ttl()})
    redis.expire(key, int(_ttl()) + 60)
    return token


def refresh(user_id, token: str) -> None:
    client().zadd(_key(user_id), {token: time.time() + _ttl()})


def release(user_id, token: str) -> None:
    client().zrem(_key(user_id), token)