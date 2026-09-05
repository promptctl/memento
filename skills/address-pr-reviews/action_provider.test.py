#!/usr/bin/env python3
"""Tests for the action provider's reviewed-verdict.

`wait` is where the address-pr-reviews loop learns whether the head commit was
reviewed. The GitHub Action reviewer exits 0 on a spent round cap by design and
says so on the PR with a marked not-reviewed review, so a run conclusion alone
reads a capped push as a clean review with zero findings — the shape that once
merged unreviewed code. Every test below asserts the VERDICT for a shape of
reviews on the PR, never how it was computed. [LAW:behavior-not-structure]

The pure verdict is exercised on data; the one end-to-end case fakes `gh` at
the subprocess seam (`github_threads.subprocess`) so argv construction, JSONL
splitting and the run poll all run for real. No test touches the network.

Run: python3 action_provider.test.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import action_provider as ap  # noqa: E402
import github_threads as gt  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def verdict(name, reviews, sha, expected):
    got = ap.head_review_verdict(reviews, sha)
    check(name, got == expected, f"got {got!r}, want {expected!r}")


# --- fixtures: what the reviewer actually leaves on a PR --------------------

REVIEW_TAIL = "<!-- copirate-code-review-agent:cost-usd:12.5 -->\n\n<!-- copirate-code-review-agent -->"
CAPPED_TAIL = ("⚠️ **NOT REVIEWED** — this action did not review this pull request.\n\n"
               "<!-- copirate-code-review-agent:not-reviewed:round-cap -->")
FORK_TAIL = "<!-- copirate-code-review-agent:not-reviewed:fork -->"
RELEASE_FAILED_TAIL = "<!-- copirate-code-review-agent:release-failed -->"


def review(rid, commit, tail, state="COMMENTED"):
    return {"review_id": rid, "author": "github-actions[bot]", "commit_id": commit,
            "state": state, "body": f"## Reviewer\n\n{tail}\n"}


REVIEWED = {"reviewed": True, "not_reviewed_reason": None}


def not_reviewed(reason):
    return {"reviewed": False, "not_reviewed_reason": reason}


# --- the verdict --------------------------------------------------------------

verdict("newest artifact is a review of the head → reviewed",
        [review(1, "aaa", REVIEW_TAIL), review(2, "bbb", REVIEW_TAIL)], "bbb", REVIEWED)

verdict("capped push: notice posted for this head → round-cap",
        [review(1, "aaa", REVIEW_TAIL), review(2, "bbb", CAPPED_TAIL)], "bbb",
        not_reviewed("round-cap"))

verdict("second capped push posts nothing new (upstream dedup) → still round-cap",
        [review(1, "aaa", REVIEW_TAIL), review(2, "bbb", CAPPED_TAIL)], "ccc",
        not_reviewed("round-cap"))

verdict("cap raised and re-run: a review of the head outranks the older notice",
        [review(2, "bbb", CAPPED_TAIL), review(3, "ccc", REVIEW_TAIL)], "ccc", REVIEWED)

verdict("fork notice passes its reason through verbatim",
        [review(1, "aaa", FORK_TAIL)], "aaa", not_reviewed("fork"))

verdict("newest artifact reviews an OLDER commit → no-review-for-head",
        [review(1, "aaa", REVIEW_TAIL)], "bbb", not_reviewed("no-review-for-head"))

verdict("no artifact at all → no-review-for-head",
        [], "aaa", not_reviewed("no-review-for-head"))

verdict("newest is by review id, not list order",
        [review(9, "ccc", REVIEW_TAIL), review(2, "bbb", CAPPED_TAIL)], "ccc", REVIEWED)

verdict("a bot review that is not an artifact (release-failed) does not displace the newest",
        [review(2, "bbb", CAPPED_TAIL), review(3, "bbb", RELEASE_FAILED_TAIL)], "bbb",
        not_reviewed("round-cap"))

verdict("a marker quoted mid-body is nobody's artifact",
        [review(2, "bbb", CAPPED_TAIL),
         review(3, "bbb", f"looks like `{ap.REVIEW_MARKER}` got posted?\n\nanyway")], "bbb",
        not_reviewed("round-cap"))

verdict("the adversarial provider's marker is not this reviewer's artifact",
        [review(1, "aaa", "<!-- adversarial-review sha=aaa -->")], "aaa",
        not_reviewed("no-review-for-head"))

verdict("a dismissed review still counts as the review it was",
        [review(1, "aaa", REVIEW_TAIL, state="DISMISSED")], "aaa", REVIEWED)

verdict("trailing whitespace after the marker is tolerated, as upstream tolerates it",
        [{**review(1, "aaa", REVIEW_TAIL), "body": f"x\n{ap.REVIEW_MARKER}\n\n  "}], "aaa", REVIEWED)


# --- wait() end to end through a fake gh -----------------------------------------

class FakeGh:
    PIPE = -1

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def check_output(self, argv, text=True, stderr=None):
        assert argv[0] == "gh", argv
        args = list(argv[1:])
        self.calls.append(args)
        return self.handler(args)


def capped_pr(head, reviews):
    def handler(args):
        path = args[1]
        if path == "repos/o/r/pulls/7":
            return head
        if path.startswith("repos/o/r/actions/workflows/code-review.yml/runs?head_sha="):
            assert path.endswith(f"head_sha={head}&per_page=1"), path
            return json.dumps([{"status": "completed", "conclusion": "success",
                                "html_url": "https://run/1"}])
        if args[:2] == ["api", "--paginate"] and args[2] == "repos/o/r/pulls/7/reviews":
            # JSONL across pages, as gh --jq emits it; body newlines stay escaped.
            return "\n".join(json.dumps(r) for r in reviews)
        raise AssertionError(f"unexpected gh call: {args}")
    return handler


fake = FakeGh(capped_pr("ccc", [review(1, "aaa", REVIEW_TAIL), review(2, "bbb", CAPPED_TAIL)]))
gt.subprocess = fake
got = ap.wait("https://github.com/o/r/pull/7")
check("wait: a green run for a capped head comes back reviewed=False with the cap's reason",
      got == {"status": "completed", "conclusion": "success", "sha": "ccc",
              "url": "https://run/1", "reviewed": False, "not_reviewed_reason": "round-cap"},
      f"got {got!r}")

fake = FakeGh(capped_pr("ccc", [review(2, "bbb", CAPPED_TAIL), review(3, "ccc", REVIEW_TAIL)]))
gt.subprocess = fake
got = ap.wait("https://github.com/o/r/pull/7")
check("wait: a reviewed head comes back reviewed=True",
      got["reviewed"] is True and got["not_reviewed_reason"] is None, f"got {got!r}")

got = gt.change_requests("https://github.com/o/r/pull/7")
check("change_requests: derives from the same bot-review read, filtered to CHANGES_REQUESTED",
      got == {"reviews": []}, f"got {got!r}")
fake = FakeGh(capped_pr("ccc", [review(2, "bbb", REVIEW_TAIL, state="CHANGES_REQUESTED")]))
gt.subprocess = fake
got = gt.change_requests("https://github.com/o/r/pull/7")
check("change_requests: keeps the contract's projection",
      got == {"reviews": [{"review_id": 2, "author": "github-actions[bot]", "commit_id": "bbb"}]},
      f"got {got!r}")

print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
