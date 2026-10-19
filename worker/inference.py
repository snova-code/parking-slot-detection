# worker/inference.py
"""ONNX Runtime inference.

Single-threaded by default: the ms/slot figure in RESULTS.md was measured that
way, so the deployed worker matches what was published. Raising APMS_ORT_THREADS
makes the deployment faster than the number you reported — note it if you do.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)


class ModelMismatch(RuntimeError):
    pass


class Classifier:
    def __init__(self, session, input_name: str, input_size: tuple[int, int],
                 version: str) -> None:
        self.session = session
        self.input_name = input_name
        self.input_size = input_size
        self.version = version

    @classmethod
    def load(cls, path: Path, input_size: tuple[int, int], version: str,
             threads: int = 1) -> "Classifier":
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = threads
        session = ort.InferenceSession(str(path), options,
                                       providers=["CPUExecutionProvider"])
        spec = session.get_inputs()[0]
        shape = list(spec.shape)
        if len(shape) != 4:
            raise ModelMismatch(f"expected a 4-D input, model wants {shape}")
        height, width = shape[2], shape[3]
        if (isinstance(height, int) and isinstance(width, int)
                and (height, width) != input_size):
            # The model card and the graph disagree; the crops would be the
            # wrong size and every prediction meaningless.
            raise ModelMismatch(
                f"model {version} takes {height}x{width} but its card says "
                f"{input_size[0]}x{input_size[1]}")
        logger.info("loaded %s (input %s, %d thread(s))", version, shape, threads)
        return cls(session, spec.name, input_size, version)

    def probabilities(self, batch: np.ndarray) -> np.ndarray:
        if batch.size == 0:
            return np.empty(0, dtype=np.float32)
        output = self.session.run(None, {self.input_name: batch})[0]
        scores = np.asarray(output, dtype=np.float32).reshape(-1)
        if scores.shape[0] != batch.shape[0]:
            raise ModelMismatch(
                f"model returned {scores.shape[0]} scores for {batch.shape[0]} crops")
        if scores.min() < -1e-4 or scores.max() > 1 + 1e-4:
            # The export is supposed to end in softmax. Raw logits here would
            # make every threshold comparison nonsense.
            raise ModelMismatch(
                f"model {self.version} returned values outside [0, 1] "
                f"({scores.min():.3f}..{scores.max():.3f}); is this the "
                "probability-output export?")
        return scores