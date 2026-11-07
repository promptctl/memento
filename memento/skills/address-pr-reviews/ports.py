"""The ports a provider is built with, and the one place the real ones are built.

Each entry point calls `production_ports()`; a test builds `Ports` over the fakes in `fakes.py`. A
new port is a field here and a line in `production_ports`, not an edit at every entry point.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import github_threads
from github_threads import GitHub


class Clock(Protocol):
    """Elapsed time and waiting. `now` is monotonic seconds: a deadline measures elapsed time, and
    wall time can jump."""

    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class Ports:
    github: GitHub
    clock: Clock


def production_ports() -> Ports:
    """The real gh and the real clock."""
    return Ports(GitHub(github_threads.run_gh), SystemClock())
