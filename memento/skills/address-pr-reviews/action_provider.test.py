#!/usr/bin/env python3
"""Tests for the action provider's body findings and their merge with review threads.

Findings the reviewer could not post inline appear in the CHANGES_REQUESTED review body, and `fetch`
must return them with the threads. `gh` is faked at the subprocess seam, so argv construction and
JSONL parsing run for real, and no test uses the network. Run with: python3 action_provider.test.py
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


# --- a fake gh at the subprocess seam ----------------------------------------

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


# --- body findings: what the reviewer could not post inline -----------------
# A finding whose line is not in the PR's diff is rendered in the review body under a fixed heading.
# Each case asserts the whole finding that a body yields.

# A real review body from PR #7, used as a fixture.
PR7_BODY = (
    "## CoPirate Code Review\n\n"
    "This PR renames the memento.conf setting from `context_ceiling` to `ceiling`.\n\n"
    "Reviewed 2 scope(s): config-key-rename-hook, release-docs-and-versioning.\n"
    "**convergence sweep 1** — nothing new; the review converged.\n\n"
    "### Findings outside the reviewed diff\n"
    "These reference lines not present in this PR's diff, so they could not be posted "
    "as inline comments:\n\n"
    "- `README.md:211` — **[S1]** Comment mismatch: this line still reads "
    "\"`memento--v0.4.0` for the current release,\" but this PR bumps to 0.5.0 — "
    "update the example to `memento--v0.5.0`.\n\n"
    "❌ Request Changes\n\n"
    "_Reviewed by config `auto→claude-subscription`._\n\n"
    "<!-- copirate-code-review-agent -->"
)


def find_body(name, body, expected, author="github-actions[bot]"):
    got = ap.parse_body_findings(body, author)
    check(name, got == expected, f"got {got!r}, want {expected!r}")


def one(file, line, text, author="github-actions[bot]"):
    return {"file": file, "line_start": line, "line_end": line, "body": text,
            "author": author, "thread_id": None, "is_resolved": False,
            "thread_comments": [{"author": author, "body": text}]}


find_body(
    "the observed out-of-diff finding is surfaced with its anchor and severity",
    PR7_BODY,
    [one("README.md", 211,
         "**[S1]** Comment mismatch: this line still reads \"`memento--v0.4.0` for "
         "the current release,\" but this PR bumps to 0.5.0 — update the example "
         "to `memento--v0.5.0`.")])

find_body(
    "the host-refused heading is read the same as the out-of-diff one",
    "### Findings the host would not post inline\n"
    "These were anchored to lines in the reviewed diff, but the host refused them:\n\n"
    "- `src/a.py:9` — **[S2]** stale anchor after the head moved.",
    [one("src/a.py", 9, "**[S2]** stale anchor after the head moved.")])

find_body(
    "every item in a section is a finding",
    "### Findings outside the reviewed diff\nexplanation line\n\n"
    "- `a.py:1` — **[S3]** first\n"
    "- `b.py:2` — **[S1]** second",
    [one("a.py", 1, "**[S3]** first"), one("b.py", 2, "**[S1]** second")])

find_body(
    "the coverage section is not a finding section",
    "### Findings outside the reviewed diff\nexpl\n\n- `a.py:1` — **[S1]** real\n\n"
    "### Changed files NOT reviewed\nThese could not be reviewed:\n\n"
    "- `big.bin` — binary file too large",
    [one("a.py", 1, "**[S1]** real")])

find_body(
    "a bullet before any findings heading is not a finding",
    "## CoPirate Code Review\n\n- this is a summary bullet, not a finding\n\n"
    "### Findings outside the reviewed diff\nexpl\n\n- `a.py:1` — **[S1]** real",
    [one("a.py", 1, "**[S1]** real")])

find_body(
    "a heading of any level ends the section: bullets under a later ## are not findings",
    "### Findings outside the reviewed diff\nexpl\n\n- `a.py:1` — **[S1]** real\n\n"
    "## Notes\n\n- a trailing note bullet, not a finding",
    [one("a.py", 1, "**[S1]** real")])

find_body(
    "a path bearing colons keeps them; the numeric tail is the line",
    "### Findings outside the reviewed diff\nx\n\n- `a:b.py:10` — **[S1]** t",
    [one("a:b.py", 10, "**[S1]** t")])

find_body(
    "a file-level body finding (bare path, no line) parses with a null line",
    "### Findings outside the reviewed diff\nx\n\n- `README.md` — **[S1]** whole-file note",
    [one("README.md", None, "**[S1]** whole-file note")])

# An item the grammar does not recognize is still returned, with a null anchor.
find_body(
    "an unparseable item is surfaced with a null anchor, not dropped",
    "### Findings outside the reviewed diff\nx\n\n- a finding with no code span at all",
    [one(None, None, "a finding with no code span at all")])

find_body("a body with no findings section yields nothing", PR7_BODY.replace(
    "### Findings outside the reviewed diff", "### Nothing to see"), [])
find_body("an empty body yields nothing", "", [])
find_body("a heading with no items yields nothing",
          "### Findings outside the reviewed diff\njust the explanation, no items", [])


# --- body_findings: scoped to the blocking reviews, aggregated across them ------

def body_review(rid, state, body):
    return {"review_id": rid, "author": "github-actions[bot]", "commit_id": "aaa",
            "state": state, "body": body}


FINDING_BODY = ("### Findings outside the reviewed diff\nx\n\n"
                "- `a.py:1` — **[S1]** blocking finding")

check("body_findings: reads the CHANGES_REQUESTED reviews and skips the rest",
      ap.body_findings([
          body_review(1, "CHANGES_REQUESTED", FINDING_BODY),
          body_review(2, "COMMENTED", FINDING_BODY),
          body_review(3, "DISMISSED", FINDING_BODY),
          body_review(4, "CHANGES_REQUESTED",
                      FINDING_BODY.replace("a.py:1", "b.py:2")),
      ]) == [one("a.py", 1, "**[S1]** blocking finding"),
             one("b.py", 2, "**[S1]** blocking finding")],
      "only the two blocking reviews' body findings, in review order")


# --- fetch: inline threads and body findings rejoin into one stream -------------

def thread_node(tid, body):
    return {"id": tid, "isResolved": False, "path": "inline.py", "line": 7,
            "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                         "nodes": [{"author": {"login": "github-actions[bot]"},
                                    "body": body}]}}


def combined_pr(thread_nodes, reviews):
    def handler(args):
        joined = " ".join(args)
        if args[:2] == ["api", "graphql"] and "reviewThreads(" in joined:
            return json.dumps({"data": {"repository": {"pullRequest": {
                "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                  "nodes": thread_nodes}}}}})
        if args[:2] == ["api", "--paginate"] and args[2] == "repos/o/r/pulls/7/reviews":
            return "\n".join(json.dumps(r) for r in reviews)
        raise AssertionError(f"unexpected gh call: {args}")
    return handler


gt.subprocess = FakeGh(combined_pr(
    [thread_node("t1", "an inline finding on a diff line")],
    [body_review(9, "CHANGES_REQUESTED", FINDING_BODY)]))
findings = ap.fetch("https://github.com/o/r/pull/7")["findings"]
check("fetch: the inline thread finding is present, keyed by its thread id",
      any(f["thread_id"] == "t1" for f in findings), f"got {findings!r}")
check("fetch: the body finding is present, thread-less, alongside the thread",
      one("a.py", 1, "**[S1]** blocking finding") in findings, f"got {findings!r}")
check("fetch: exactly the two findings, threads then body",
      [f["thread_id"] for f in findings] == ["t1", None], f"got {findings!r}")


# --- setup_check: reads the workflow list; absent is not installed, a gh failure raises ---

def workflow_list(matches):
    """A gh whose workflow list, filtered to code-review.yml by the jq, emits `matches`."""
    def handler(args):
        assert args[:3] == ["api", "--paginate", "repos/o/r/actions/workflows?per_page=100"], args
        assert '.path == ".github/workflows/code-review.yml"' in args[4], args
        return "\n".join(json.dumps({"state": s}) for s in matches)
    return handler


for label, matches, installed, mention in [
    ("active is installed", ["active"], True, "is active"),
    ("absent is not installed, pointing at setup", [], False, "run the agent-code-review-setup skill"),
    ("disabled is not installed, naming the state", ["disabled_manually"], False, "disabled_manually"),
]:
    gt.subprocess = FakeGh(workflow_list(matches))
    check_result = ap.setup_check("o", "r")
    check(f"setup_check: {label}",
          check_result["installed"] is installed and mention in check_result["message"],
          f"got {check_result!r}")


def repo_not_found(args):
    raise gt.CalledProcessError(1, ["gh", *args], stderr="gh: Not Found (HTTP 404)\n",
                                output='{"message":"Not Found","status":"404"}')


gt.subprocess = FakeGh(repo_not_found)
try:
    check_result = ap.setup_check("o", "r")
except gt.GhError as e:
    check("setup_check: an unreadable repo raises, not 'not installed'",
          "Not Found (HTTP 404)" in str(e) and "'message': 'Not Found'" in str(e), f"message {str(e)!r}")
else:
    check("setup_check: an unreadable repo raises, not 'not installed'", False,
          f"returned {check_result!r}")


print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
