# worker/api_client.py
"""HTTP to the server. The only way the worker reaches anything server-side:
it never touches Postgres or Redis (02_ARCHITECTURE.md rule 1).

Errors are split by what the worker should do about them. A bad token will not
become good by retrying and should stop the process with a clear message; a
5xx or a dropped connection should be retried.
"""
from __future__ import annotations

import logging

import httpx

from worker.config import Settings
from worker.contract import WorkerConfig

logger = logging.getLogger(__name__)
USER_AGENT = "apms-worker/0.1"


class ApiError(RuntimeError):
    """Base class. `retryable` decides whether the caller should back off or
    give up."""

    retryable = True

    def __init__(self, message: str, *, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class AuthRejected(ApiError):
    retryable = False


class ContractViolation(ApiError):
    retryable = False


def build_http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=f"{settings.server_url}/api/v1",
        timeout=settings.http_timeout,
        headers={
            "Authorization": f"Bearer {settings.device_token}",
            "User-Agent": USER_AGENT,
        },
        follow_redirects=False,
    )


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    code, message = "", response.text[:200]
    try:
        error = response.json().get("error", {})
        code, message = error.get("code", ""), error.get("message", message)
    except ValueError:
        pass
    if response.status_code in (401, 403):
        raise AuthRejected(f"server rejected this token: {message}",
                           code=code, status=response.status_code)
    raise ApiError(f"server returned {response.status_code}: {message}",
                   code=code, status=response.status_code)


class ApiClient:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def fetch_config(self) -> WorkerConfig:
        try:
            response = self.http.get("/worker/config")
        except httpx.HTTPError as exc:
            raise ApiError(f"could not reach the server: {exc}") from exc
        _raise_for_status(response)
        try:
            return WorkerConfig.model_validate(response.json())
        except ValueError as exc:
            # The server answered, but not with something this worker can run.
            raise ContractViolation(f"config did not match the contract: {exc}") from exc