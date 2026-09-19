#!/usr/bin/env python3
"""Tests for `review wait` in bin/review.

`cmd_wait` turns a run the loop must not trust — a reviewer that errored —
into a nonzero exit. The verdict is asserted on the (result, failure) pair
for each shape a provider can return, never on how the branch is spelled.
[LAW:behavior-not-structure]

Run: python3 review_cli.test.py
"""

import importlib.machinery
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "review_cli", os.path.join(HERE, "bin", "review"),
    loader=importlib.machinery.SourceFileLoader("review_cli", os.path.join(HERE, "bin", "review")),
)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


PR = "https://github.com/o/r/pull/7"
ARGS = types.SimpleNamespace(pr=PR)


def provider_returning(result):
    return types.SimpleNamespace(wait=lambda url: {**result, "url": "https://run/9"})


def wait_with(**fields):
    result = {"status": "completed", "sha": "ccc", **fields}
    return cli.cmd_wait(provider_returning(result), ARGS)


got, failure = wait_with(conclusion="success")
check("success: no failure", failure is None and got["sha"] == "ccc", f"got {failure!r}")

got, failure = wait_with(conclusion="failure")
check("non-success conclusion: fails naming the conclusion and the run",
      failure is not None and "'failure'" in failure and "https://run/9" in failure,
      f"got {failure!r}")

print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
