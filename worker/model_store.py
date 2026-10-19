# worker/model_store.py
"""The ONNX model cache.

Fetched once and kept by version (04_API_CONTRACT.md: "Do not re-download
every restart"). The sha256 from the config is checked on every start, not
only after downloading: a truncated file from a killed container is exactly
the kind of thing that would otherwise produce quietly wrong predictions.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx

from worker.contract import ModelSpec

logger = logging.getLogger(__name__)
CHUNK = 1 << 20


class ModelIntegrityError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


class ModelStore:
    def __init__(self, directory: Path, http: httpx.Client) -> None:
        self.directory = directory
        self.http = http
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, spec: ModelSpec) -> Path:
        return self.directory / f"{spec.version}.onnx"

    def ensure(self, spec: ModelSpec) -> Path:
        target = self.path_for(spec)
        if target.exists():
            found = sha256_of(target)
            if found == spec.sha256:
                logger.info("model %s already cached", spec.version)
                return target
            logger.warning("cached model %s is corrupt (%s), refetching",
                           spec.version, found[:12])
            target.unlink()
        self._download(spec, target)
        return target

    def _download(self, spec: ModelSpec, target: Path) -> None:
        partial = target.with_suffix(".part")
        digest = hashlib.sha256()
        logger.info("downloading model %s", spec.version)
        try:
            with self.http.stream("GET", spec.url, timeout=120.0) as response:
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    for block in response.iter_bytes(CHUNK):
                        digest.update(block)
                        handle.write(block)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise ModelIntegrityError(f"could not download {spec.url}: {exc}") from exc

        if digest.hexdigest() != spec.sha256:
            partial.unlink(missing_ok=True)
            raise ModelIntegrityError(
                f"model {spec.version} failed verification: expected "
                f"{spec.sha256[:12]}, got {digest.hexdigest()[:12]}")
        partial.replace(target)     # atomic: no half file is ever loadable
        logger.info("model %s cached (%.1f MB)", spec.version,
                    target.stat().st_size / 1e6)