"""The in-memory fakes of the ports in `ports.py`, shared by every test that builds a provider.

`FakeRunner` stands in for github_threads' Runner. A test teaches it gh's outcomes through a handler
from argv (without the leading `gh`) to one of: the stdout of a gh that succeeded, `failed(...)` for
one that exited nonzero, or an OSError for one that could not be started. A handler raises
AssertionError for an argv it was not taught.
"""

from __future__ import annotations

from subprocess import CompletedProcess
from typing import Callable, Sequence, Union

from github_threads import Outcome

# What a handler returns: a gh's stdout is shorthand for the Outcome of a gh that succeeded.
Answer = Union[str, Outcome]


def failed(stderr: str, stdout: str = "") -> CompletedProcess[str]:
    """A gh that exited 1, having printed `stdout` (an API response body, say) and `stderr`."""
    return CompletedProcess([], 1, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Records each argv in `calls` and answers it with what the handler returns.

    It refuses more calls than any correct loop could make, which turns a runaway pagination loop
    into a failed test instead of a hung suite.
    """

    CALL_CAP = 25

    def __init__(self, handler: Callable[[list[str]], Answer]):
        self.handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> Outcome:
        args = list(args)
        self.calls.append(args)
        if len(self.calls) > self.CALL_CAP:
            raise AssertionError(f"runaway pagination: {len(self.calls)} gh calls")
        outcome = self.handler(args)
        if isinstance(outcome, str):
            return CompletedProcess(args, 0, stdout=outcome, stderr="")
        return outcome


class FakeClock:
    """Time that moves only when told, and only forward. `sleep` advances `now` and records the
    interval in `sleeps`; `advance` moves time without a sleep, as a gh call that takes time would."""

    def __init__(self):
        self._now = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        # A monotonic clock never runs backwards, so a test that scripts it to is wrong.
        if seconds < 0:
            raise AssertionError(f"clock moved backwards by {-seconds}s at t={self._now}")
        self._now += seconds
