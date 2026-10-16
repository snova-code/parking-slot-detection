# ml/apms_ml/provenance.py
"""Provenance stamps for result rows.

A row is dirty when the code that produced it is not committed. Files under
ml/results/ are excluded from that check: they are the outputs being written,
and counting them would mark every row after the first in a multi-split run as
dirty.

Untracked files elsewhere DO count. A new, uncommitted source file means the
code behind the row cannot be recovered from the recorded commit.
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from .paths import REPO_ROOT

OUTPUTS_EXCLUDED = ":(exclude)ml/results"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def git_commit() -> str:
    try:
        sha = _git("rev-parse", "--short", "HEAD")
        dirty = _git("status", "--porcelain", "--", ".", OUTPUTS_EXCLUDED)
        return f"{sha}-dirty" if dirty else sha
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def host_cpu() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"