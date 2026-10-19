# worker/tests/test_model_store.py
"""Model caching and integrity."""
from __future__ import annotations

import hashlib

import httpx
import pytest

from worker.contract import ModelSpec
from worker.model_store import ModelIntegrityError, ModelStore

PAYLOAD = b"not really an onnx file, but it hashes like one"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def spec(sha: str = DIGEST) -> ModelSpec:
    return ModelSpec(version="mnv3s-test", url="https://models.example/m.onnx",
                     sha256=sha, input_size=(96, 96))


def serving(body: bytes, calls: list) -> httpx.Client:
    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_model_is_downloaded_once(tmp_path):
    calls = []
    store = ModelStore(tmp_path, serving(PAYLOAD, calls))
    first = store.ensure(spec())
    second = store.ensure(spec())
    assert first == second and first.read_bytes() == PAYLOAD
    assert len(calls) == 1


def test_a_corrupt_cache_is_refetched(tmp_path):
    calls = []
    store = ModelStore(tmp_path, serving(PAYLOAD, calls))
    path = store.ensure(spec())
    path.write_bytes(b"truncated")

    assert store.ensure(spec()).read_bytes() == PAYLOAD
    assert len(calls) == 2


def test_a_wrong_hash_is_never_left_on_disk(tmp_path):
    store = ModelStore(tmp_path, serving(b"something else", []))
    with pytest.raises(ModelIntegrityError):
        store.ensure(spec())
    assert list(tmp_path.iterdir()) == []


def test_a_failed_download_leaves_nothing_behind(tmp_path):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    store = ModelStore(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ModelIntegrityError):
        store.ensure(spec())
    assert list(tmp_path.iterdir()) == []