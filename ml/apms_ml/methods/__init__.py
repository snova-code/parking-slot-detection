# ml/apms_ml/methods/__init__.py
"""Method registry.

A method takes a list of BGR patches and returns one score per patch in [0, 1],
where higher means more likely OCCUPIED. Hard-label methods return 0.0 or 1.0.

Registering by name keeps evaluate.py free of per-method branching, so adding
a method is one new file plus one line at the bottom of this one.

ORDER IS LOAD-BEARING. Method modules do `from . import register`, which reads
this module while it is still executing. Every name they need must be defined
above the side-effect imports at the end of the file. Moving those imports up
raises `ImportError: cannot import name 'register' from partially initialized
module`. Do not add an import of a method module anywhere but the final block.
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


# Side-effect imports. Must stay last. Each module calls register() at import
# time. One line per method, no duplicates — a duplicate import is harmless
# (sys.modules caches it) but reads as a second registration that never happens.
from . import threshold_a  # noqa: E402,F401
from . import mobilenet_b  # noqa: E402,F401