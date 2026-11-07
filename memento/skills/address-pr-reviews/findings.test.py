#!/usr/bin/env python3
"""Tests for findings.fetch: every finding comes back placed against the PR's head.

The action provider runs over a fake gh, so the thread query, the review read, and the head read all
run for real against staged answers. No test uses the network. Run with: python3 findings.test.py
"""

import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import action_provider as ap  # noqa: E402
import findings  # noqa: E402
import github_threads as gt  # noqa: E402
import provider_loader  # noqa: E402
from fakes import FakeClock, FakeRunner  # noqa: E402
from ports import Ports  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


PR = "https://github.com/o/r/pull/7"
HEAD_READ = ["api", "repos/o/r/pulls/7", "--jq", ".head.sha"]


def thread(tid, commit):
    return {"id": tid, "isResolved": False, "path": "a.py", "originalLine": 7,
            "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                         "nodes": [{"author": {"login": "github-actions[bot]"}, "body": tid,
                                    "originalCommit": commit and {"oid": commit}}]}}


BLOCKING_ON_OLD = {
    "review_id": 9, "author": "github-actions[bot]", "commit_id": "old",
    "state": "CHANGES_REQUESTED",
    "body": "### Findings outside the reviewed diff\nx\n\n- `b.py:3` — **[S1]** a body finding",
}


def pr(threads, reviews, head):
    """A gh for PR o/r#7. `head` is called with the calls made so far, so a test can move the head
    partway through a fetch, as a push would."""
    calls = []

    def handler(args):
        calls.append(args)
        if args == HEAD_READ:
            return head(calls)
        if args[:2] == ["api", "graphql"] and "reviewThreads(" in " ".join(args):
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": threads}}}}})
        if args[:3] == ["api", "--paginate", "repos/o/r/pulls/7/reviews"]:
            return "\n".join(json.dumps(r) for r in reviews)
        raise AssertionError(f"unexpected gh call: {args}")
    return ap.Provider(Ports(gt.GitHub(FakeRunner(handler)), FakeClock()))


# --- the ticket's shape: a review on an older commit, and a newer head ---------

got = findings.fetch(
    pr([thread("on-old", "old"), thread("on-new", "new"), thread("no-commit", None)],
       [BLOCKING_ON_OLD], head=lambda calls: "new"),
    PR, "new")
check("the head the findings were placed against is returned",
      got["head_sha"] == "new", f"got {got['head_sha']!r}")
check("findings from the older review read as older; the one on the head as head; the one with no commit unknown",
      [(f["body"], f["commit_id"], f["against_head"]) for f in got["findings"]] == [
          ("on-old", "old", "older"),
          ("on-new", "new", "head"),
          ("no-commit", None, "unknown"),
          ("**[S1]** a body finding", "old", "older"),
      ], f"got {got['findings']!r}")


# --- a push between reading the findings and reading the head ------------------
# The head is at `old` while the findings are read, and moves to `new` before the head is read. The
# findings on `old` are no longer on the head, so they must not read as being on it.


def pushed_after_findings(calls):
    findings_read = any(c[:3] == ["api", "--paginate", "repos/o/r/pulls/7/reviews"] for c in calls)
    return "new" if findings_read else "old"


got = findings.fetch(pr([thread("on-old", "old")], [BLOCKING_ON_OLD], pushed_after_findings), PR, "old")
check("a push during the fetch leaves findings on the superseded head reading as older",
      [f["against_head"] for f in got["findings"]] == ["older", "older"] and got["head_sha"] == "new",
      f"got {got!r}")
check("a push during the fetch leaves the loop not done", got["done"] is False, f"got {got!r}")


# --- the done rule: the head is the commit the wait reviewed, and nothing is unresolved ----------
# The ticket's shape: wait's review covered `old`, a push moved the head to `new`, and `new`'s review
# has not run, so nothing is pending yet. An empty list here must not read as a clean head.

got = findings.fetch(pr([], [], head=lambda calls: "new"), PR, "old")
check("no findings on a head the wait did not review: not done",
      got["findings"] == [] and got["head_sha"] == "new" and got["done"] is False,
      f"got {got!r}")

got = findings.fetch(pr([], [], head=lambda calls: "old"), PR, "old")
check("no findings on the head the wait reviewed: done",
      got["findings"] == [] and got["done"] is True, f"got {got!r}")

got = findings.fetch(pr([thread("on-old", "old")], [], head=lambda calls: "old"), PR, "old")
check("an unresolved finding on the head the wait reviewed: not done", got["done"] is False, f"got {got!r}")

got = findings.fetch(pr([{**thread("on-old", "old"), "isResolved": True}], [], head=lambda calls: "old"),
                     PR, "old")
check("only resolved findings on the head the wait reviewed: done",
      len(got["findings"]) == 1 and got["done"] is True, f"got {got!r}")


# --- every provider supplies the head -------------------------------------------

no_head = types.SimpleNamespace(
    CAPABILITIES={c: False for c in provider_loader.REQUIRED_CAPABILITIES},
    wait=lambda url: None, fetch=lambda url: None)
try:
    provider_loader._validate(no_head, "nohead")
except RuntimeError as e:
    check("a provider with no head_sha is refused, naming the operation", "'head_sha'" in str(e),
          f"got {e}")
else:
    check("a provider with no head_sha is refused, naming the operation", False, "accepted")

print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
