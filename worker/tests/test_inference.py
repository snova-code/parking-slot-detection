# worker/tests/test_inference.py
"""The classifier's guards against loading the wrong export."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from worker.inference import Classifier, ModelMismatch


class FakeSession:
    def __init__(self, scores) -> None:
        self.scores = np.asarray(scores, dtype=np.float32)

    def run(self, _outputs, feed):
        count = next(iter(feed.values())).shape[0]
        return [self.scores[:count].reshape(-1, 1)]


def classifier(scores) -> Classifier:
    return Classifier(FakeSession(scores), "patches_bgr_0_255", (96, 96), "test")


def batch(count: int) -> np.ndarray:
    return np.zeros((count, 3, 96, 96), dtype=np.float32)


def test_probabilities_come_back_flat():
    assert classifier([0.1, 0.9]).probabilities(batch(2)).tolist() == \
        pytest.approx([0.1, 0.9])


def test_an_empty_batch_needs_no_session():
    assert classifier([]).probabilities(batch(0)).shape == (0,)


def test_logit_output_is_refused():
    """A model exported without the final softmax would make every threshold
    comparison meaningless, silently."""
    with pytest.raises(ModelMismatch, match="outside"):
        classifier([4.2, -3.1]).probabilities(batch(2))


def test_a_short_answer_is_refused():
    with pytest.raises(ModelMismatch, match="scores"):
        classifier([0.5]).probabilities(batch(2))