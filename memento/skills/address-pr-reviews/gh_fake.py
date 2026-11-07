"""The in-memory fake of github_threads' Runner, shared by every test that drives a `GitHub` client.

A test teaches it gh's outcomes through a handler from argv (without the leading `gh`) to one of:
the stdout of a gh that succeeded, `failed(...)` for one that exited nonzero, or an OSError for one
that could not be started. A handler raises AssertionError for an argv it was not taught.
"""

from __future__ import annotations

from subprocess import CompletedProcess
from typing import Callable, Sequence

Outcome = "str | CompletedProcess[str] | OSError"


def failed(stderr: str, stdout: str = "") -> CompletedProcess[str]:
    """A gh that exited 1, having printed `stdout` (an API response body, say) and `stderr`."""
    return CompletedProcess([], 1, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Records each argv in `calls` and answers it with what the handler returns.

    It refuses more calls than any correct loop could make, which turns a runaway pagination loop
    into a failed test instead of a hung suite.
    """

    CALL_CAP = 25

    def __init__(self, handler: Callable[[list[str]], Outcome]):
        self.handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> CompletedProcess[str] | OSError:
        args = list(args)
        self.calls.append(args)
        if len(self.calls) > self.CALL_CAP:
            raise AssertionError(f"runaway pagination: {len(self.calls)} gh calls")
        outcome = self.handler(args)
        if isinstance(outcome, str):
            return CompletedProcess(args, 0, stdout=outcome, stderr="")
        return outcome
