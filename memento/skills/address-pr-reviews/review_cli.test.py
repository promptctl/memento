#!/usr/bin/env python3
"""Tests for `wait` and `reply` in bin/review.

A reviewer run that errored, and a reply GitHub did not confirm, must each become a nonzero exit.
Run with: python3 review_cli.test.py
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
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

# --- reply: SKILL.md posts every plan and fix comment through `bin/review reply` ----------------


class ReplyGh:
    """Stands in for github_threads' subprocess module, answering the reply mutation with `response`."""

    PIPE = -1

    def __init__(self, response):
        self.response = response
        self.calls = []

    def check_output(self, argv, text=True, stderr=None):
        self.calls.append(argv)
        return self.response


def run_reply(response, *argv):
    fake = ReplyGh(response)
    cli.github_threads.subprocess = fake
    sys.argv = ["review", *argv]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main()
    return code, out.getvalue(), err.getvalue(), fake.calls


POSTED = json.dumps({"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "PRRC_1"}}}})
UNPOSTED = json.dumps({"data": {"addPullRequestReviewThreadReply": None}})

code, out, err, calls = run_reply(POSTED, "--provider", "action", "reply", "T_abc", "--body", "Plan: x")
check("reply: a posted comment prints its id and exits 0",
      code == 0 and json.loads(out) == {"thread_id": "T_abc", "comment_id": "PRRC_1"}
      and "id=T_abc" in calls[0] and "body=Plan: x" in calls[0],
      f"code={code} out={out!r} err={err!r} calls={calls!r}")

code, out, err, _ = run_reply(UNPOSTED, "--provider", "action", "reply", "T_abc", "--body", "Plan: x")
check("reply: no comment id exits 1 saying NOT posted",
      code == 1 and "NOT posted" in err, f"code={code} err={err!r}")

code, out, err, calls = run_reply(POSTED, "--provider", "local", "reply", "T_abc", "--body", "Plan: x")
check("reply: a provider without resolve is refused before any gh call",
      code == 1 and "CAPABILITIES['resolve'] is False" in err and not calls,
      f"code={code} err={err!r} calls={calls!r}")

print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
