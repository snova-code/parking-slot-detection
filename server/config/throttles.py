# server/config/throttles.py
"""Rate limits from 04_API_CONTRACT.md.

Counters live in the Redis cache so they are shared across gunicorn workers.
Throttles run after authentication, so a worker limit can be keyed on the
camera the token identified — one noisy worker cannot spend another's
allowance.
"""
from __future__ import annotations

from django.conf import settings
from ninja.throttling import SimpleRateThrottle


def client_ip(request) -> str:
    """REMOTE_ADDR unless a proxy we control is in front. The rightmost
    X-Forwarded-For entry is the one that proxy appended; everything to its
    left came from the client and can say anything."""
    if settings.TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


class _ScopedThrottle(SimpleRateThrottle):
    """The scope is part of the cache key: without it, two throttles on the
    same camera would share one bucket."""

    def __init__(self, scope_name: str, rate: str) -> None:
        self.scope_name = scope_name
        super().__init__(rate)


class PerCameraThrottle(_ScopedThrottle):
    def get_cache_key(self, request):
        camera = getattr(getattr(request, "auth", None), "camera", None)
        return None if camera is None else f"throttle:{self.scope_name}:camera:{camera.pk}"


class PerIpThrottle(_ScopedThrottle):
    def get_cache_key(self, request):
        return f"throttle:{self.scope_name}:ip:{client_ip(request)}"


worker_events_throttle = PerCameraThrottle("worker_events", settings.RATE_WORKER_EVENTS)
worker_heartbeat_throttle = PerCameraThrottle("worker_heartbeat", settings.RATE_WORKER_HEARTBEAT)
worker_config_throttle = PerCameraThrottle("worker_config", settings.RATE_WORKER_CONFIG)
login_throttle = PerIpThrottle("login", settings.RATE_LOGIN)