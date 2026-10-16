"""Method B — the exported MobileNetV3-Small, run through ONNX Runtime.

Preprocessing here is resize only. Channel order, scaling and normalisation are
inside the graph, so this adapter has nothing it can get wrong.

Model selection: $APMS_ONNX_MODEL, else the newest models/*.onnx.

Two sessions are held. The default one uses ONNX Runtime's own thread pool for
the accuracy pass; the second is pinned to one thread so the latency figure in
results.csv means milliseconds per slot on a single core, as
05_BENCHMARK_PROTOCOL.md specifies.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from . import register
from ..paths import REPO_ROOT

DEFAULT_INPUT_SIZE = 96


def _resolve_model() -> Path:
    override = os.environ.get("APMS_ONNX_MODEL")
    if override:
        path = Path(override).expanduser()
        if not path.exists():
            raise SystemExit(f"APMS_ONNX_MODEL points at a missing file: {path}")
        return path
    candidates = sorted((REPO_ROOT / "models").glob("*.onnx"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise SystemExit(
            "No model found. Download the ONNX export into models/, or set "
            "APMS_ONNX_MODEL. Method B cannot be evaluated without one."
        )
    return candidates[0]


class MobileNetOnnx:
    name = "mobilenet_b"

    def __init__(self) -> None:
        self.path = _resolve_model()
        self.model_size_bytes = self.path.stat().st_size

        card_path = self.path.with_suffix(".json")
        if card_path.exists():
            card = json.loads(card_path.read_text(encoding="utf-8"))
            self.version = card.get("version", self.path.stem)
            self.input_size = int(card["input"]["shape"][2])
        else:
            print(f"WARNING: no model card beside {self.path.name}. "
                  f"Assuming {DEFAULT_INPUT_SIZE}x{DEFAULT_INPUT_SIZE} input.")
            self.version = self.path.stem
            self.input_size = DEFAULT_INPUT_SIZE

        self._session = ort.InferenceSession(str(self.path),
                                             providers=["CPUExecutionProvider"])
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._session_1t = ort.InferenceSession(str(self.path), options,
                                                providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name

    def _batch(self, patches: list[np.ndarray]) -> np.ndarray:
        size = (self.input_size, self.input_size)
        resized = [cv2.resize(p, size, interpolation=cv2.INTER_LINEAR) for p in patches]
        stacked = np.stack(resized).transpose(0, 3, 1, 2)
        return np.ascontiguousarray(stacked, dtype=np.float32)

    def _run(self, session, patches: list[np.ndarray]) -> np.ndarray:
        output = session.run(None, {self._input_name: self._batch(patches)})[0]
        return np.asarray(output, dtype=np.float32).reshape(-1)

    def predict(self, patches: list[np.ndarray]) -> np.ndarray:
        return self._run(self._session, patches)

    def predict_single_thread(self, patches: list[np.ndarray]) -> np.ndarray:
        return self._run(self._session_1t, patches)


register("mobilenet_b", MobileNetOnnx)