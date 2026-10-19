# worker/config.py
"""Worker settings, entirely from the environment.

The worker holds no configuration of its own beyond how to reach the server
and where to keep its cache. Everything about the camera — slots, model,
smoothing, sampling — comes from GET /worker/config, so a restart cannot run
on stale local state.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(RuntimeError):
    pass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigurationError(f"{name} is not set")
    return value or ""


@dataclass(frozen=True)
class Settings:
    server_url: str
    # repr=False: this must not reach a log line, a traceback or a crash report.
    device_token: str = field(repr=False)
    data_dir: Path
    log_level: str
    http_timeout: float
    ort_threads: int

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"


def load_settings() -> Settings:
    settings = Settings(
        server_url=_env("APMS_SERVER_URL", required=True).rstrip("/"),
        device_token=_env("APMS_DEVICE_TOKEN", required=True),
        data_dir=Path(_env("APMS_DATA_DIR", "./worker-data")).expanduser(),
        log_level=_env("APMS_LOG_LEVEL", "INFO").upper(),
        http_timeout=float(_env("APMS_HTTP_TIMEOUT", "10")),
        ort_threads=int(_env("APMS_ORT_THREADS", "1")),
    )
    if not settings.device_token.startswith("apms_dev_"):
        raise ConfigurationError("APMS_DEVICE_TOKEN does not look like a device token")
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    return settings