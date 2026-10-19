# worker/tests/test_api_client.py
"""Config fetching, and what the worker does about each kind of failure."""
from __future__ import annotations

import httpx
import pytest

from worker.api_client import ApiClient, ApiError, AuthRejected, ContractViolation

CONFIG = {
    "camera_id": "3f2a0000-0000-4000-8000-000000000001",
    "lot_name": "Labim Mall",
    "source_uri": "file:///data/demo.mp4",
    "reference_frame": {"width": 1280, "height": 720},
    "model": {"version": "mnv3s-test", "url": "https://models.example/m.onnx",
              "sha256": "ab" * 32, "input_size": [96, 96]},
    "smoothing": {"window": 9, "min_agreement": 6},
    "sampling": {"target_fps": 2},
    "slots": [{"id": "a91c0000-0000-4000-8000-000000000001", "label": "A-01",
               "polygon": [[120, 340], [186, 338], [190, 382], [124, 384]]}],
    "config_version": "9c1f3a2b8d4e5f60",
}


def client_returning(response: httpx.Response) -> ApiClient:
    return ApiClient(httpx.Client(transport=httpx.MockTransport(lambda r: response),
                                  base_url="http://server/api/v1"))


def test_a_valid_config_is_parsed():
    config = client_returning(httpx.Response(200, json=CONFIG)).fetch_config()
    assert config.slots[0].label == "A-01"
    assert config.model.input_size == (96, 96)
    assert config.smoothing.min_agreement == 6


def test_a_rejected_token_is_not_retryable():
    response = httpx.Response(401, json={"error": {"code": "unauthenticated",
                                                   "message": "Unauthorized",
                                                   "detail": {}}})
    with pytest.raises(AuthRejected) as caught:
        client_returning(response).fetch_config()
    assert caught.value.retryable is False


def test_an_unavailable_model_is_retryable():
    response = httpx.Response(503, json={"error": {"code": "model_unavailable",
                                                   "message": "…", "detail": {}}})
    with pytest.raises(ApiError) as caught:
        client_returning(response).fetch_config()
    assert caught.value.retryable is True
    assert caught.value.code == "model_unavailable"


@pytest.mark.parametrize("broken", [
    {"slots": [{"id": "a91c0000-0000-4000-8000-000000000001", "label": "A-01",
                "polygon": [[1, 2], [3, 4], [5, 6]]}]},
    {"smoothing": {"window": 5, "min_agreement": 9}},
    {"model": {**CONFIG["model"], "sha256": "nonsense"}},
], ids=["three-cornered-slot", "agreement-over-window", "bad-sha256"])
def test_a_config_this_worker_cannot_run_is_fatal(broken):
    with pytest.raises(ContractViolation):
        client_returning(httpx.Response(200, json={**CONFIG, **broken})).fetch_config()