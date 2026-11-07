#!/usr/bin/env python3
"""GitHub Action PR review provider for the coding-agent-review reviewer.

The reviewer runs on pull_request and posts a formal review whose inline comments are ordinary
resolvable review threads. Findings it cannot anchor to a diff line appear in its CHANGES_REQUESTED
review body under a fixed heading. `fetch` returns both forms as one list, and `wait` tracks the
workflow run for the current head SHA.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import github_threads
from github_threads import GhError
from ports import Ports

# The workflow file this provider watches.
WORKFLOW_FILE = "code-review.yml"

REGISTER_TIMEOUT_S = 300
COMPLETION_TIMEOUT_S = 3600
POLL_INTERVAL_S = 8
# How long every runs poll may fail, measured from the last one that succeeded (or the start of the
# wait), before wait gives up on gh rather than on the review: long enough to ride out a network
# reconnect or a short GitHub incident, and longer than one killed gh call.
OUTAGE_TIMEOUT_S = 300


# ---------------------------------------------------------------------------
# Findings the reviewer could not post inline
# ---------------------------------------------------------------------------

# Headings the reviewer uses for findings it could not post inline. They are matched literally and
# copied from copirate-code-review-agent src/transport.js.
BODY_FINDING_HEADINGS = frozenset({
    "### Findings outside the reviewed diff",
    "### Findings the host would not post inline",
})

# One item line as the reviewer writes it: `- ` + a code span of `path:line` + ` — ` + the body. The
# code span's backtick fence may be padded when the path contains a backtick. Each finding is one
# physical line; a `- ` line this regex rejects is still returned, with a null anchor, never skipped.
_BODY_ITEM_RE = re.compile(r"^-\s+(`+) ?(.*?) ?\1 — (.*)$")


def _split_path_line(span: str) -> tuple[Optional[str], Optional[int]]:
    """Splits `path:line` into (path, line). Colons earlier in the path are kept. A span with no
    numeric tail returns (span, None)."""
    head, sep, tail = span.rpartition(":")
    if sep and tail.isdigit():
        return head, int(tail)
    return span, None


def parse_body_findings(body: str, author: str) -> list[dict]:
    """Returns the findings listed under body-finding headings in a review body. Returns [] if there are none.

    A body finding has no thread, so `thread_id` is None and it is cleared by dismissing its review,
    not by `resolve`.
    """
    findings: list[dict] = []
    in_section = False
    for line in body.splitlines():
        # Any ATX heading ends a section, so a `- ` bullet under a later heading is not read as a finding.
        if re.match(r"#{1,6} ", line):
            in_section = line.rstrip() in BODY_FINDING_HEADINGS
            continue
        if not (in_section and line.startswith("- ")):
            continue
        m = _BODY_ITEM_RE.match(line)
        file, line_no, text = (
            (*_split_path_line(m.group(2)), m.group(3)) if m
            else (None, None, line[2:])
        )
        findings.append({
            "file":            file,
            "line_start":      line_no,
            "line_end":        line_no,
            "body":            text,
            "author":          author,
            "thread_id":       None,
            "is_resolved":     False,
            "thread_comments": [{"author": author, "body": text}],
        })
    return findings


def body_findings(reviews: list[dict]) -> list[dict]:
    """Returns every body finding across the blocking reviews in `reviews` (the `bot_reviews` shape).

    Blocking is decided by `github_threads.is_blocking_review`, the same predicate `change_requests`
    uses. A dismissed review no longer blocks, so its findings are no longer returned."""
    return [
        f
        for r in reviews if github_threads.is_blocking_review(r)
        for f in parse_body_findings(r["body"], r["author"])
    ]


# ---------------------------------------------------------------------------
# The provider: the contract operations over its ports
# ---------------------------------------------------------------------------

class Provider:
    CAPABILITIES = {
        "resolve":        True,   # GitHub review threads are resolvable
        "trigger":        False,  # fires automatically on push via GitHub Action
        "setup_check":    True,   # checks that code-review.yml workflow is installed
        "dismiss_review": True,   # github-actions posts a dismissible CHANGES_REQUESTED review
    }

    def __init__(self, ports: Ports):
        self._github = ports.github
        # [LAW:effects-at-boundaries] wait reads time only through this port, so a test drives its
        # deadlines on a fake clock.
        self._clock = ports.clock
        # Taken from the client unchanged. resolve applies to inline threads only, since a body
        # finding has no thread; it is cleared by dismiss_review.
        self.resolve = self._github.resolve
        self.change_requests = self._github.change_requests
        self.dismiss_review = self._github.dismiss_review

    def fetch(self, pr_url: str) -> dict:
        """Returns every pending finding: inline review threads plus body findings from blocking
        reviews."""
        data = self._github.fetch(pr_url)
        data["findings"].extend(body_findings(self._github.bot_reviews(pr_url)))
        return data

    def setup_check(self, owner: str, repo: str) -> dict:
        """Verify code-review.yml workflow is installed on the repo.

        The repo's workflow list is read rather than the workflow itself, because GitHub answers 404
        for a missing workflow, a missing repo, and a repo the token cannot see alike. Here an absent
        workflow is an empty match, and every gh failure raises. [LAW:no-silent-failure]
        """
        state = next((w["state"] for w in self._github.paginated(
            f"repos/{owner}/{repo}/actions/workflows?per_page=100",
            f'.workflows[] | select(.path == ".github/workflows/{WORKFLOW_FILE}") | {{state}}',
        )), None)
        if state is None:
            return {
                "installed": False,
                "message": (
                    f"review workflow ({WORKFLOW_FILE}) not found on "
                    f"{owner}/{repo} — run the agent-code-review-setup skill in this "
                    "repo and merge it to the default branch first."
                ),
            }
        if state == "active":
            return {"installed": True, "message": f"{WORKFLOW_FILE} is active"}
        return {
            "installed": False,
            "message": (
                f"{WORKFLOW_FILE} exists but state is '{state}' — "
                "check Actions settings on this repo."
            ),
        }

    def _latest_run(self, owner: str, repo: str, sha: str) -> Optional[dict]:
        out = self._github.gh(
            "api",
            f"repos/{owner}/{repo}/actions/workflows/{WORKFLOW_FILE}/runs"
            f"?head_sha={sha}&per_page=1",
            "--jq", ".workflow_runs",
        )
        runs = json.loads(out) if out else []
        return runs[0] if runs else None

    def wait(self, pr_url: str) -> dict:
        """Block until the GitHub Action run for the current head SHA completes.

        A failed runs poll is no news: the wait goes on to the same deadlines, and gives up on gh only
        once every poll for OUTAGE_TIMEOUT_S has failed. Each failure it rode out is returned in
        `poll_errors`. [LAW:no-silent-failure]
        """
        owner, repo, pr_num = github_threads.parse_pr(pr_url)
        sha = self._github.head_sha(owner, repo, pr_num)
        start = last_ok = self._clock.now()
        # The last run any poll listed. A run that registered is held to the completion deadline
        # even if a later poll misses it, so a gap in the run list is not reported as "never
        # registered".
        run: Optional[dict] = None
        poll_errors: list[str] = []
        while True:
            # This pass's poll failure, if it failed.
            error: Optional[GhError] = None
            try:
                run = self._latest_run(owner, repo, sha) or run
                last_ok = self._clock.now()
            except GhError as e:
                error = e
                poll_errors.append(str(e))
            if run and run.get("status") == "completed":
                return {
                    "status":      "completed",
                    "conclusion":  run.get("conclusion"),
                    "sha":         sha,
                    "url":         run.get("html_url"),
                    "poll_errors": poll_errors,
                }
            now = self._clock.now()
            # [LAW:no-ambient-temporal-coupling] every bound is seconds on the one clock, so a gh that
            # fails slowly uses up the outage window sooner instead of stretching it.
            if now - last_ok >= OUTAGE_TIMEOUT_S:
                raise RuntimeError(
                    f"every review-run poll for {sha} failed for {now - last_ok:.0f}s "
                    f"(the limit is {OUTAGE_TIMEOUT_S}s); the last: {error}"
                ) from error
            deadline = COMPLETION_TIMEOUT_S if run else REGISTER_TIMEOUT_S
            if now - start >= deadline:
                break
            self._clock.sleep(POLL_INTERVAL_S)
        # A deadline raise names the polls that failed, so a flaky gh is not read as a missing workflow.
        failed_polls = (
            f" Polls that failed along the way: {len(poll_errors)}; the last: {poll_errors[-1]}"
            if poll_errors else ""
        )
        if run is None:
            raise RuntimeError(
                f"No review run ({WORKFLOW_FILE}) registered for {sha} within "
                f"{REGISTER_TIMEOUT_S}s. Is the workflow installed on this repo "
                f"(run the agent-code-review-setup skill) and are Actions enabled?{failed_polls}"
            ) from error
        raise RuntimeError(
            f"review run for {sha} did not complete within {COMPLETION_TIMEOUT_S}s "
            f"(status: {run.get('status')}). The runner may be wedged: {run.get('html_url')}"
            f"{failed_polls}"
        ) from error


# ---------------------------------------------------------------------------
# CLI shim so the provider can run directly. The skill loads it through provider_loader.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    from ports import production_ports

    provider = Provider(production_ports())
    parser = argparse.ArgumentParser(description="GitHub Action PR review provider (direct)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("wait", "fetch"):
        p = sub.add_parser(name)
        p.add_argument("pr_url")
        p.set_defaults(func=getattr(provider, name))
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("thread_id")
    p_resolve.set_defaults(func=provider.resolve)

    args = parser.parse_args()
    try:
        if args.command in ("wait", "fetch"):
            print(json.dumps(args.func(args.pr_url), indent=2))
        else:
            print(json.dumps(args.func(args.thread_id), indent=2))
    except (RuntimeError, ValueError) as e:
        print(f"ERROR ({args.command}): {e}", file=sys.stderr)
        sys.exit(1)
