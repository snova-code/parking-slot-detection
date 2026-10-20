# worker/tests/test_model_store.py
"""Model caching and integrity."""
from __future__ import annotations

import hashlib

import httpx
import pytest

from worker import model_store
from worker.contract import ModelSpec
from worker.model_store import ModelIntegrityError, ModelStore

PAYLOAD = b"not really an onnx file, but it hashes like one"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def spec(sha: str = DIGEST) -> ModelSpec:
    return ModelSpec(version="mnv3s-test", url="https://models.example/m.onnx",
                     sha256=sha, input_size=(96, 96))


@pytest.fixture
def host(monkeypatch):
    """Installs a fake model host. Returns the list of requests it saw."""
    def install(handler):
        calls = []

        def wrapped(request):
            assert not any(name.lower() == "authorization" for name in request.headers), \
                "the device token must never reach the model host"
            calls.append(request.url)
            return handler(request)

        monkeypatch.setattr(
            model_store, "_download_client",
            lambda: httpx.Client(transport=httpx.MockTransport(wrapped)))
        return calls

    return install


def ok(body: bytes):
    return lambda request: httpx.Response(200, content=body)


def test_a_model_is_downloaded_once(tmp_path, host):
    calls = host(ok(PAYLOAD))
    store = ModelStore(tmp_path)
    first = store.ensure(spec())
    second = store.ensure(spec())
    assert first == second and first.read_bytes() == PAYLOAD
    assert len(calls) == 1


def test_a_corrupt_cache_is_refetched(tmp_path, host):
    calls = host(ok(PAYLOAD))
    store = ModelStore(tmp_path)
    store.ensure(spec()).write_bytes(b"truncated")
    assert store.ensure(spec()).read_bytes() == PAYLOAD
    assert len(calls) == 2


def test_a_wrong_hash_is_never_left_on_disk(tmp_path, host):
    host(ok(b"something else"))
    with pytest.raises(ModelIntegrityError):
        ModelStore(tmp_path).ensure(spec())
    assert list(tmp_path.iterdir()) == []


def test_a_404_leaves_nothing_behind(tmp_path, host):
    host(lambda request: httpx.Response(404, text="Not Found"))
    with pytest.raises(ModelIntegrityError, match="could not download"):
        ModelStore(tmp_path).ensure(spec())
    assert list(tmp_path.iterdir()) == []


def test_a_dropped_connection_leaves_nothing_behind(tmp_path, host):
    def drop(request):
        raise httpx.ConnectError("no route to host")

    host(drop)
    with pytest.raises(ModelIntegrityError):
        ModelStore(tmp_path).ensure(spec())
    assert list(tmp_path.iterdir()) == []