#!/usr/bin/env python3
"""Tests for the action provider's body findings and their merge with review threads.

Findings the reviewer could not post inline appear in the CHANGES_REQUESTED review body, and `fetch`
must return them with the threads. The provider is built over fake ports, so argv construction and
JSONL parsing run for real, no test uses the network, and `wait` runs on a clock that never sleeps. Run with: python3 action_provider.test.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import action_provider as ap  # noqa: E402
import github_threads as gt  # noqa: E402
from fakes import FakeClock, FakeRunner, failed  # noqa: E402
from ports import Ports  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def provider(handler, clock=None):
    return ap.Provider(Ports(gt.GitHub(FakeRunner(handler)), clock or FakeClock()))


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


pr_with_both = provider(combined_pr(
    [thread_node("t1", "an inline finding on a diff line")],
    [body_review(9, "CHANGES_REQUESTED", FINDING_BODY)]))
findings = pr_with_both.fetch("https://github.com/o/r/pull/7")["findings"]
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
    check_result = provider(workflow_list(matches)).setup_check("o", "r")
    check(f"setup_check: {label}",
          check_result["installed"] is installed and mention in check_result["message"],
          f"got {check_result!r}")


def repo_not_found(args):
    return failed("gh: Not Found (HTTP 404)\n", stdout='{"message":"Not Found","status":"404"}')


try:
    check_result = provider(repo_not_found).setup_check("o", "r")
except gt.GhError as e:
    check("setup_check: an unreadable repo raises, not 'not installed'",
          "Not Found (HTTP 404)" in str(e) and "'message': 'Not Found'" in str(e), f"message {str(e)!r}")
else:
    check("setup_check: an unreadable repo raises, not 'not installed'", False,
          f"returned {check_result!r}")


# --- wait: polls the head SHA's run until it completes, or until the deadline for its state ------

RUN_URL = "https://github.com/o/r/actions/runs/5"
RUNS_ARGV = ["api", "repos/o/r/actions/workflows/code-review.yml/runs?head_sha=ccc&per_page=1",
             "--jq", ".workflow_runs"]


def run(status, conclusion=None):
    return {"status": status, "conclusion": conclusion, "html_url": RUN_URL}


def wait_over(polls):
    """Runs `wait` on a PR whose head SHA is `ccc` and whose run list answers `polls` in order.

    Each poll is (runs, t): the runs listed, and the clock time the call returns at, as a gh call
    that takes time would. A poll past the last one raises AssertionError. Returns (the result or the
    exception, the clock, the poll times)."""
    clock = FakeClock()
    remaining = list(polls)
    poll_times = []

    def handler(args):
        if args == ["api", "repos/o/r/pulls/7", "--jq", ".head.sha"]:
            return "ccc"
        if args != RUNS_ARGV:
            raise AssertionError(f"unexpected gh call {args!r}")
        poll_times.append(clock.now())
        if not remaining:
            raise AssertionError(f"poll {len(poll_times)} at t={clock.now()} is past the {len(polls)} scripted")
        runs, t = remaining.pop(0)
        clock.advance(t - clock.now())
        return json.dumps(runs)

    try:
        return provider(handler, clock).wait("https://github.com/o/r/pull/7"), clock, poll_times
    except (RuntimeError, AssertionError) as e:
        return e, clock, poll_times


got, clock, _ = wait_over([([run("completed", "success")], 0)])
check("wait: a run already completed returns it without sleeping",
      got == {"status": "completed", "conclusion": "success", "sha": "ccc", "url": RUN_URL}
      and clock.sleeps == [], f"got {got!r}, sleeps {clock.sleeps!r}")

got, clock, _ = wait_over([([run("queued")], 1), ([run("in_progress")], 10),
                           ([run("completed", "failure")], 20)])
check("wait: polls every POLL_INTERVAL_S until the run completes, and returns its conclusion",
      isinstance(got, dict) and got["conclusion"] == "failure"
      and clock.sleeps == [ap.POLL_INTERVAL_S] * 2, f"got {got!r}, sleeps {clock.sleeps!r}")

# A poll before the register deadline keeps polling; one that returns at exactly the deadline stops.
# A wait that held a missing run to the completion deadline, or that stopped only past the register
# deadline, would poll a third time.
got, clock, poll_times = wait_over([([], 200), ([], ap.REGISTER_TIMEOUT_S)])
check("wait: no run registered by REGISTER_TIMEOUT_S raises, naming the SHA and the deadline",
      isinstance(got, RuntimeError) and "ccc" in str(got)
      and f"{ap.REGISTER_TIMEOUT_S}s" in str(got) and len(poll_times) == 2
      and clock.sleeps == [ap.POLL_INTERVAL_S], f"got {got!r}, polls at {poll_times!r}")

# The run first appears past the register deadline, so only the completion deadline, measured from
# the start of the wait, may stop the loop. The last poll returns at exactly COMPLETION_TIMEOUT_S: a
# wait that kept the register deadline would stop after two polls, and one that measured from the
# run's first sighting would poll a fourth time.
got, clock, poll_times = wait_over([([], 200), ([run("in_progress")], ap.REGISTER_TIMEOUT_S + 100),
                                    ([run("in_progress")], ap.COMPLETION_TIMEOUT_S)])
check("wait: a run not completed by COMPLETION_TIMEOUT_S raises, naming the deadline and the run",
      isinstance(got, RuntimeError) and RUN_URL in str(got)
      and f"{ap.COMPLETION_TIMEOUT_S}s" in str(got) and len(poll_times) == 3
      and clock.sleeps == [ap.POLL_INTERVAL_S] * 2, f"got {got!r}, polls at {poll_times!r}")


print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
