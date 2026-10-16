# server/config/settings.py
"""Django settings.

Every secret and environment-specific value comes from the environment. A
missing required variable stops startup instead of falling back to an insecure
default — there is no committed SECRET_KEY anywhere, including examples.

Only what exists today is configured. Sessions, middleware and URL routing are
added with the tasks that need them.
"""
from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(
            f"{name} is not set. Run through scripts/with_env.sh, or export it."
        )
    return value or ""


SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
DEBUG = env("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "accounts",
    "parking",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", required=True),
        "USER": env("POSTGRES_USER", required=True),
        "PASSWORD": env("POSTGRES_PASSWORD", required=True),
        "HOST": env("POSTGRES_HOST", required=True),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

# Must be set before the first migrate. Changing it afterwards means
# rewriting migration history.
AUTH_USER_MODEL = "accounts.User"

USE_TZ = True
TIME_ZONE = "UTC"          # stored in UTC; each ParkingLot carries its own zone
USE_I18N = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"