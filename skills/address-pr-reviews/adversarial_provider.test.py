#!/usr/bin/env python3
"""Tests for the adversarial provider's `wait` contract.

`wait` is synchronous here — `trigger` already posted — so its whole job is
to prove the head was reviewed: the marker review for the head SHA exists, or
it raises. The loop reads `reviewed` off the result to decide whether an
empty `fetch` means clean, so the field must ride on the proof.
[LAW:behavior-not-structure] asserted on the returned contract, through a
fake `gh` at the subprocess seam.

Run: python3 adversarial_provider.test.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import adversarial_provider as ap  # noqa: E402
import github_threads as gt  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


class FakeGh:
    PIPE = -1

    def __init__(self, head, reviews):
        self.head, self.reviews = head, reviews

    def check_output(self, argv, text=True, stderr=None):
        assert argv[0] == "gh", argv
        args = argv[1:]
        if args[1] == "repos/o/r/pulls/7":
            return self.head
        if args[1].startswith("repos/o/r/pulls/7/reviews"):
            return json.dumps(self.reviews)
        raise AssertionError(f"unexpected gh call: {args}")


PR = "https://github.com/o/r/pull/7"
HEAD, OLD = "c" * 7, "b" * 7  # the marker grammar wants a real-length SHA


def marker_review(sha):
    return {"body": f"## Adversarial review\n\nfine\n\n{ap.MARKER_FMT.format(sha=sha)}\n",
            "html_url": f"https://r/{sha}", "state": "COMMENTED"}


gt.subprocess = FakeGh(HEAD, [marker_review(OLD), marker_review(HEAD)])
got = ap.wait(PR)
check("wait: a marker review for the head proves it reviewed",
      got == {"status": "completed", "conclusion": "success", "sha": HEAD,
              "url": f"https://r/{HEAD}", "reviewed": True, "not_reviewed_reason": None},
      f"got {got!r}")

gt.subprocess = FakeGh(HEAD, [marker_review(OLD)])
try:
    ap.wait(PR)
except RuntimeError as e:
    check("wait: no marker review for the head raises, never reports unreviewed-as-clean",
          HEAD in str(e) and "trigger" in str(e), f"message {str(e)!r}")
else:
    check("wait: no marker review for the head raises", False, "did not raise")

print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
