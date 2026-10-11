"""Method registry.

A method takes a list of BGR patches and returns one score per patch in [0, 1],
where higher means more likely OCCUPIED. Hard-label methods return 0.0 or 1.0.

Registering by name keeps evaluate.py free of per-method branching, so adding
Method B in Task 1.3 touches one line here and one new file.
"""
from __future__ import annotations

from typing import Callable, Protocol

import numpy as np


class Method(Protocol):
    name: str
    version: str
    model_size_bytes: int

    def predict(self, patches: list[np.ndarray]) -> np.ndarray:
        """Scores in [0, 1], one per patch, same order."""


_FACTORIES: dict[str, Callable[[], Method]] = {}


def register(name: str, factory: Callable[[], Method]) -> None:
    if name in _FACTORIES:
        raise ValueError(f"method {name!r} already registered")
    _FACTORIES[name] = factory


def build(name: str) -> Method:
    if name not in _FACTORIES:
        raise SystemExit(f"Unknown method {name!r}. Available: {', '.join(available())}")
    return _FACTORIES[name]()


def available() -> list[str]:
    return sorted(_FACTORIES)


from . import threshold_a  # noqa: E402,F401  (import registers the method)