#!/usr/bin/env python3
"""Tests for `wait`, `fetch`, and `reply` in bin/review.

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
sys.path.insert(0, HERE)
import github_threads  # noqa: E402
from fakes import FakeClock, FakeRunner, failed  # noqa: E402
from ports import Ports  # noqa: E402

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
    result = {"status": "completed", "sha": "ccc", "poll_errors": [], **fields}
    return cli.cmd_wait(provider_returning(result), None, ARGS)


got, failure = wait_with(conclusion="success")
check("success: no failure", failure is None and got["sha"] == "ccc", f"got {failure!r}")

got, failure = wait_with(conclusion="failure")
check("non-success conclusion: fails naming the conclusion and the run",
      failure is not None and "'failure'" in failure and "https://run/9" in failure,
      f"got {failure!r}")

# --- reply: SKILL.md posts every plan and fix comment through `bin/review reply` ----------------


def run_cli(handler, *argv):
    """Runs `review <argv>` with a gh that answers each call with what `handler` returns for it."""
    fake = FakeRunner(handler)
    sys.argv = ["review", *argv]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(Ports(github_threads.GitHub(fake), FakeClock()))
        except SystemExit as e:
            if e.code != 2:  # 2 is argparse refusing the arguments; any other exit is a bug in main
                raise
            code = e.code
    return code, out.getvalue(), err.getvalue(), fake.calls


POSTED = json.dumps({"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "PRRC_1"}}}})
UNPOSTED = json.dumps({"data": {"addPullRequestReviewThreadReply": None}})

code, out, err, calls = run_cli(lambda _: POSTED, "--provider", "action", "reply", "T_abc", "--body", "Plan: x")
check("reply: a posted comment prints its id and exits 0",
      code == 0 and json.loads(out) == {"thread_id": "T_abc", "comment_id": "PRRC_1"}
      and "id=T_abc" in calls[0] and "body=Plan: x" in calls[0],
      f"code={code} out={out!r} err={err!r} calls={calls!r}")

code, out, err, _ = run_cli(lambda _: UNPOSTED, "--provider", "action", "reply", "T_abc", "--body", "Plan: x")
check("reply: no comment id exits 1 saying NOT posted",
      code == 1 and "NOT posted" in err, f"code={code} err={err!r}")

GH_STDERR = "GraphQL: Could not resolve to a node with the global id of 'T_abc'. (node)"
GH_FAILED = failed(GH_STDERR + "\n")
code, out, err, _ = run_cli(lambda _: GH_FAILED, "--provider", "action", "reply", "T_abc", "--body", "Plan: x")
check("reply: a failed gh call exits 1 printing gh's stderr",
      code == 1 and err == f"ERROR (reply): gh api graphql exited 1: {GH_STDERR}\n",
      f"code={code} err={err!r}")

# The client main builds is the one the provider runs through, so a provider command reaches the fake too.
def workflow_list(args):
    if args[:3] != ["api", "--paginate", "repos/o/r/actions/workflows?per_page=100"]:
        raise AssertionError(f"unexpected gh call: {args}")
    return '{"state": "active"}'


code, out, err, calls = run_cli(workflow_list, "--provider", "action", "setup-check", "o", "r")
check("setup-check: the provider's gh call goes through the runner main was given",
      code == 0 and json.loads(out)["installed"] is True and len(calls) == 1,
      f"code={code} out={out!r} err={err!r} calls={calls!r}")

def pr_with_one_thread(args):
    if args == ["api", "repos/o/r/pulls/7", "--jq", ".head.sha"]:
        return "new"
    if args[:2] == ["api", "graphql"]:
        return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"id": "T_1", "isResolved": False, "path": "a.py", "originalLine": 1, "comments": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"author": {"login": "bot"}, "body": "x",
                           "originalCommit": {"oid": "old"}}]}}]}}}}})
    if args[:3] == ["api", "--paginate", "repos/o/r/pulls/7/reviews"]:
        return ""
    raise AssertionError(f"unexpected gh call: {args}")


def waited(conclusion):
    """The JSON `review wait` prints, for a review of a commit that is not the head."""
    return json.dumps({"status": "completed", "conclusion": conclusion, "sha": "0" * 40,
                       "url": "https://run", "poll_errors": []})


code, out, err, _ = run_cli(pr_with_one_thread, "--provider", "action", "fetch", PR, "--review", waited("success"))
got = json.loads(out) if code == 0 else None
check("fetch: prints the head, whether the loop is done, and each finding placed against it",
      got is not None and got["head_sha"] == "new" and got["done"] is False
      and [f["against_head"] for f in got["findings"]] == ["older"],
      f"code={code} out={out!r} err={err!r}")

code, out, err, calls = run_cli(pr_with_one_thread, "--provider", "action", "fetch", PR)
check("fetch: refused without the review wait printed, before any gh call",
      code == 2 and "--review" in err and not calls, f"code={code} err={err!r} calls={calls!r}")

for name, given, says in [("a failed review", waited("failure"), "not success"),
                          ("a --review that is not JSON", "0" * 40, "ERROR (fetch)")]:
    code, out, err, calls = run_cli(pr_with_one_thread, "--provider", "action", "fetch", PR, "--review", given)
    check(f"fetch: {name} exits 1, saying why, before any gh call",
          code == 1 and says in err and not calls, f"code={code} err={err!r} calls={calls!r}")

code, out, err, calls = run_cli(lambda _: POSTED, "--provider", "local", "reply", "T_abc", "--body", "Plan: x")
check("reply: a provider without resolve is refused before any gh call",
      code == 1 and "CAPABILITIES['resolve'] is False" in err and not calls,
      f"code={code} err={err!r} calls={calls!r}")

print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
