# server/config/settings.py
"""Django settings.

Every secret and environment-specific value comes from the environment. A
missing required variable stops startup instead of falling back to an insecure
default — there is no committed SECRET_KEY anywhere, including examples.
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


def env_flag(name: str) -> bool:
    return env(name, "false").lower() == "true"


def env_list(name: str) -> list[str]:
    return [item.strip() for item in env(name).split(",") if item.strip()]


# --- Worker configuration, served by GET /worker/config ----------------------
# Defaults exist for design choices, never for measured values. The smoothing
# pair is a result from ml/results/smoothing_sweep.md, so it has no default.


def _positive_int(name: str, default: str | None = None) -> int:
    raw = env(name, default, required=default is None)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ImproperlyConfigured(f"{name} must be at least 1, got {value}")
    return value


def _positive_float(name: str, default: str) -> float:
    raw = env(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be positive, got {value}")
    return value

SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
DEBUG = env_flag("DJANGO_DEBUG")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "accounts",
    "parking",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]

ROOT_URLCONF = "config.urls"

# Required by ninja's docs page, which renders through the Django template
# engine even when served from its CDN template.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {},
    }
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

# Must be set before the first migrate.
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {"user_attributes": ("phone_number", "full_name", "email")},
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env_flag("DJANGO_SECURE_COOKIES")
# The CSRF cookie stays readable by JavaScript (Django's default): the
# dashboard reads it to send X-CSRFToken.
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Numbers typed without a country code are read as Nepali.
PHONE_DEFAULT_REGION = "NP"

USE_TZ = True
TIME_ZONE = "UTC"
USE_I18N = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SMOOTHING_WINDOW = _positive_int("APMS_SMOOTHING_WINDOW")
SMOOTHING_MIN_AGREEMENT = _positive_int("APMS_SMOOTHING_MIN_AGREEMENT")
if SMOOTHING_MIN_AGREEMENT > SMOOTHING_WINDOW:
    raise ImproperlyConfigured(
        "APMS_SMOOTHING_MIN_AGREEMENT cannot exceed APMS_SMOOTHING_WINDOW")

# A design choice, not a measurement: occupancy changes over minutes
# (04_API_CONTRACT.md).
TARGET_FPS = _positive_float("APMS_TARGET_FPS", "2")

MODEL_URL_TEMPLATE = env("APMS_MODEL_URL_TEMPLATE", required=True)
if "{version}" not in MODEL_URL_TEMPLATE:
    raise ImproperlyConfigured("APMS_MODEL_URL_TEMPLATE must contain {version}")

# Model cards written by ml/train/train_mobilenetv3.py and committed to models/.
MODEL_CARDS_DIR = Path(env("APMS_MODEL_CARDS_DIR") or BASE_DIR.parent / "models")

# --- Live updates ------------------------------------------------------------
REDIS_URL = env("APMS_REDIS_URL", required=True)
SSE_HEARTBEAT_SECONDS = _positive_float("APMS_SSE_HEARTBEAT_SECONDS", "20")
# EventSource reconnects on its own, so capping a connection's life costs the
# client nothing and stops threads being held forever by abandoned tabs.
SSE_MAX_CONNECTION_SECONDS = _positive_float("APMS_SSE_MAX_CONNECTION_SECONDS", "1800")

# --- Camera liveness ---------------------------------------------------------
# Three missed 30s heartbeats (04_API_CONTRACT.md).
CAMERA_STALE_SECONDS = _positive_float("APMS_CAMERA_STALE_SECONDS", "90")
SWEEP_INTERVAL_SECONDS = _positive_float("APMS_SWEEP_INTERVAL_SECONDS", "15")

# --- Rate limiting -----------------------------------------------------------
# Redis-backed so counters are shared across gunicorn workers. A per-process
# cache would give each worker its own allowance.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "apms",
    }
}
TRUST_X_FORWARDED_FOR = env_flag("APMS_TRUST_X_FORWARDED_FOR")
SSE_MAX_CONNECTIONS_PER_USER = _positive_int("APMS_SSE_MAX_CONNECTIONS_PER_USER", "5")

RATE_WORKER_EVENTS = env("APMS_RATE_WORKER_EVENTS", "60/min")
RATE_WORKER_HEARTBEAT = env("APMS_RATE_WORKER_HEARTBEAT", "4/min")
RATE_WORKER_CONFIG = env("APMS_RATE_WORKER_CONFIG", "20/min")
RATE_LOGIN = env("APMS_RATE_LOGIN", "10/min")

# --- Reference frames --------------------------------------------------------
# One JPEG per camera, overwritten. The annotation tool needs what the camera
# sees now; a history of frames has no reader. On disk rather than in a column
# so that every camera query does not carry 200KB it does not want.
MEDIA_ROOT = Path(env("APMS_MEDIA_ROOT") or BASE_DIR.parent / "media")
FRAME_MAX_BYTES = _positive_int("APMS_FRAME_MAX_BYTES", str(4 * 1024 * 1024))