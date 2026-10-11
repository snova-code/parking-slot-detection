"""Confusion matrix and derived metrics.

Positive class is OCCUPIED (label 1), per 05_BENCHMARK_PROTOCOL.md.

Two error rates are reported because "false vacancy" is ambiguous and the
distinction is operationally large:

  false_vacancy_rate = FN / (FN + TN)
      Of the slots this method reports VACANT, the fraction that are actually
      occupied. This is the driver-facing number — the probability that a bay
      you were sent to is taken. It goes in the headline results table.

  miss_rate = FN / (FN + TP) = 1 - recall
      Of the slots that are actually occupied, the fraction reported vacant.
      A model-facing number, insensitive to how many vacant slots exist.

They diverge sharply when the lot is near-empty or near-full, which is exactly
when the system is being relied on.

All metrics return None rather than 0.0 when the denominator is zero, so an
undefined metric is visibly empty in results.csv instead of silently reading as
a perfect or terrible score.
"""
from __future__ import annotations

from dataclasses import dataclass


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


@dataclass
class Confusion:
    """Mutable accumulator. Positive = occupied."""

    tp: int = 0   # occupied, called occupied
    fp: int = 0   # vacant,   called occupied
    tn: int = 0   # vacant,   called vacant
    fn: int = 0   # occupied, called vacant  <- the false-vacancy events

    def update(self, truth: int, prediction: int) -> None:
        if truth == 1:
            if prediction == 1:
                self.tp += 1
            else:
                self.fn += 1
        else:
            if prediction == 1:
                self.fp += 1
            else:
                self.tn += 1

    def update_many(self, truths, predictions) -> None:
        for truth, prediction in zip(truths, predictions, strict=True):
            self.update(int(truth), int(prediction))

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def accuracy(self) -> float | None:
        return _ratio(self.tp + self.tn, self.n)

    @property
    def precision(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def specificity(self) -> float | None:
        return _ratio(self.tn, self.tn + self.fp)

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None or precision + recall == 0:
            return None
        return 2 * precision * recall / (precision + recall)

    @property
    def false_vacancy_rate(self) -> float | None:
        """FN / (FN + TN): of slots reported vacant, fraction actually occupied."""
        return _ratio(self.fn, self.fn + self.tn)

    @property
    def miss_rate(self) -> float | None:
        """FN / (FN + TP): of occupied slots, fraction reported vacant."""
        return _ratio(self.fn, self.fn + self.tp)

    @property
    def balanced_accuracy(self) -> float | None:
        recall, specificity = self.recall, self.specificity
        if recall is None or specificity is None:
            return None
        return (recall + specificity) / 2

    @property
    def occupied_share(self) -> float | None:
        return _ratio(self.tp + self.fn, self.n)

    def as_dict(self) -> dict[str, object]:
        return {
            "n_samples": self.n,
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "occupied_share": self.occupied_share,
            "accuracy": self.accuracy,
            "precision": self.precision,
            "recall": self.recall,
            "specificity": self.specificity,
            "f1": self.f1,
            "false_vacancy_rate": self.false_vacancy_rate,
            "miss_rate": self.miss_rate,
            "balanced_accuracy": self.balanced_accuracy,
        }