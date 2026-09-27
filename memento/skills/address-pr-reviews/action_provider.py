#!/usr/bin/env python3
"""GitHub Action PR review provider — implements the provider contract for the
coding-agent-review GitHub Action reviewer.

The reviewer is a GitHub Action (brandon-fryslie/coding-agent-review)
that runs on pull_request (opened, synchronize) and posts a formal PR review
with inline comments — i.e. ordinary resolvable review threads, authored by
github-actions.

Lifecycle owner is the *workflow run*, not `reviewRequests`. [LAW:no-ambient-temporal-coupling]
`wait` blocks on that owner — the run keyed to the current head SHA — never
on event-stream timing or comment counts.

Most findings land as review threads. The ones the reviewer could not anchor to a
diff line — an issue in a file this PR made stale but did not touch — it cannot post
inline, so it renders them in the CHANGES_REQUESTED review BODY instead, under a
fixed heading (copirate-code-review-agent src/transport.js, renderFindingSection).
Both are the same finding set the reviewer split by whether an anchor was accepted,
so `fetch` reconstitutes both into one canonical stream — there is no second source,
only a second rendering. [LAW:one-source-of-truth]
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Optional

# [LAW:one-source-of-truth] the Finding shape, thread read, verified resolve, and
# the blocking-review rule are the shared GitHub primitives — used here, never
# copied. resolve/change_requests/dismiss_review are re-exported unchanged; `fetch`
# is not (it is defined below, wrapping github_threads.fetch with body findings).
# Import resolution is owned by provider_loader (loaded path) or script-mode
# sys.path (direct).
import github_threads
from github_threads import (  # noqa: F401  (contract surface)
    resolve,
    change_requests,
    dismiss_review,
)

CAPABILITIES = {
    "resolve":        True,   # GitHub review threads are resolvable
    "trigger":        False,  # fires automatically on push via GitHub Action
    "setup_check":    True,   # checks that code-review.yml workflow is installed
    "dismiss_review": True,   # github-actions posts a dismissible CHANGES_REQUESTED review
}

# The workflow file this provider watches. [LAW:one-source-of-truth]
WORKFLOW_FILE = "code-review.yml"

REGISTER_TIMEOUT_S = 300
COMPLETION_TIMEOUT_S = 3600
POLL_INTERVAL_S = 8


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _latest_run(owner: str, repo: str, sha: str) -> Optional[dict]:
    out = github_threads.gh(
        "api",
        f"repos/{owner}/{repo}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?head_sha={sha}&per_page=1",
        "--jq", ".workflow_runs",
    )
    runs = json.loads(out) if out else []
    return runs[0] if runs else None


# ---------------------------------------------------------------------------
# Findings the reviewer could not post inline
# ---------------------------------------------------------------------------

# [LAW:one-source-of-truth] The reviewer's own section headings, mirrored from
# copirate-code-review-agent src/transport.js (renderUnanchoredSection /
# renderDisplacedSection). A finding whose line is not in the diff (or whose
# inline comment the host refused) reaches the reader in the review BODY under
# exactly one of these, and transport.js calls the first string "a CONTRACT, not
# a label" — the worker is told it three times as the promised destination.
# Renaming there without renaming here leaves two maps of one section disagreeing,
# so the strings are matched literally and the source is cited. "### Changed
# files NOT reviewed" is deliberately NOT here: it is a coverage report, not a
# finding.
BODY_FINDING_HEADINGS = frozenset({
    "### Findings outside the reviewed diff",
    "### Findings the host would not post inline",
})

# [LAW:parse-dont-validate] One item line as the producer wrote it:
# `- ` + codeSpan(`path:line`) + ` — ` + severity-tagged body (transport.js
# renderFindingSection). The fence is one-or-more backticks with an optional pad
# space when the path itself contains a backtick, so `+ and the optional spaces
# mirror codeSpan exactly.
#
# One finding is one physical line: transport.js runs every finding body through
# flattenBody, which collapses VERTICAL_SEPARATORS (\n \r \u2028 \u2029) to spaces
# before rendering, so a finding never wraps and there is no continuation line for
# the scan to drop. What CAN vary is a `- ` item line the reviewer's grammar shifts
# under us; that is the silent loss this parser refuses, so a `- ` line the regex
# rejects is surfaced with a null anchor below, never skipped.
_BODY_ITEM_RE = re.compile(r"^-\s+(`+) ?(.*?) ?\1 — (.*)$")


def _split_path_line(span: str) -> tuple[Optional[str], Optional[int]]:
    """`path:line` → (path, line). The line is the last colon-separated segment
    when it is all digits, so a path that itself contains colons keeps them. A
    span with no numeric tail keeps the whole thing as the path and reports no
    line rather than inventing one."""
    head, sep, tail = span.rpartition(":")
    if sep and tail.isdigit():
        return head, int(tail)
    return span, None


def parse_body_findings(body: str, author: str) -> list[dict]:
    """[LAW:effects-at-boundaries] Pure. Turn a review body into the canonical
    findings the reviewer rendered there because no diff line could anchor them —
    one per item line under a body-finding heading, empty for a body with none.

    A body finding has no thread: `thread_id` is null and it is unresolvable, so
    its disposition is the dismissal of the blocking review it lives in, not a
    `resolve` call. `is_resolved` is False for the same reason every entry here is
    pending — the body is read from a review that is still CHANGES_REQUESTED.
    """
    findings: list[dict] = []
    in_section = False
    for line in body.splitlines():
        # A section runs from its heading until the next heading of ANY level, so
        # any ATX heading ends it — the reviewer's own tail (verdict, footer,
        # markers) carries no `- ` items, and matching only `### ` would let a
        # bullet under a later `##`/`#` heading read as a phantom finding.
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
    """[LAW:effects-at-boundaries] Pure. Every body finding across the reviewer's
    blocking reviews (the `bot_reviews` shape). [LAW:single-enforcer] the blocking
    test is `github_threads.is_blocking_review` — the very predicate `change_requests`
    uses — so neither applies a different blocking rule than the other (the rule is
    single-sourced there; see its note on why sharing the predicate, not one snapshot,
    is sound). A dismissed review has left that state, so its body findings are
    disposed and no longer read. [LAW:one-source-of-truth]"""
    return [
        f
        for r in reviews if github_threads.is_blocking_review(r)
        for f in parse_body_findings(r["body"], r["author"])
    ]


def fetch(pr_url: str) -> dict:
    """Every pending finding on the PR: the inline review threads AND the
    out-of-diff findings the reviewer rendered in its blocking-review bodies.
    [LAW:one-source-of-truth] the two renderings the reviewer split its findings
    across, rejoined into the one canonical stream this loop trusts."""
    data = github_threads.fetch(pr_url)
    data["findings"].extend(body_findings(github_threads.bot_reviews(pr_url)))
    return data


# ---------------------------------------------------------------------------
# Contract: setup_check
# ---------------------------------------------------------------------------

def setup_check(owner: str, repo: str) -> dict:
    """Verify code-review.yml workflow is installed on the repo."""
    try:
        state = github_threads.gh(
            "api", f"repos/{owner}/{repo}/actions/workflows/{WORKFLOW_FILE}",
            "--jq", ".state",
        )
        if state == "active":
            return {"installed": True, "message": f"{WORKFLOW_FILE} is active"}
        return {
            "installed": False,
            "message": (
                f"{WORKFLOW_FILE} exists but state is '{state}' — "
                "check Actions settings on this repo."
            ),
        }
    except subprocess.CalledProcessError:
        return {
            "installed": False,
            "message": (
                f"review workflow ({WORKFLOW_FILE}) not found on "
                f"{owner}/{repo} — run the agent-code-review-setup skill in this "
                "repo and merge it to the default branch first."
            ),
        }


# ---------------------------------------------------------------------------
# Contract: wait
# ---------------------------------------------------------------------------

def wait(pr_url: str) -> dict:
    """Block until the GitHub Action run for the current head SHA completes."""
    owner, repo, pr_num = github_threads.parse_pr(pr_url)
    sha = github_threads.head_sha(owner, repo, pr_num)
    start = time.time()
    run: Optional[dict] = None
    while True:
        run = _latest_run(owner, repo, sha)
        if run and run.get("status") == "completed":
            return {
                "status":     "completed",
                "conclusion": run.get("conclusion"),
                "sha":        sha,
                "url":        run.get("html_url"),
            }
        deadline = COMPLETION_TIMEOUT_S if run else REGISTER_TIMEOUT_S
        if time.time() - start >= deadline:
            break
        time.sleep(POLL_INTERVAL_S)
    if run is None:
        raise RuntimeError(
            f"No review run ({WORKFLOW_FILE}) registered for {sha} within "
            f"{REGISTER_TIMEOUT_S}s. Is the workflow installed on this repo "
            "(run the agent-code-review-setup skill) and are Actions enabled?"
        )
    raise RuntimeError(
        f"review run for {sha} did not complete within {COMPLETION_TIMEOUT_S}s "
        f"(status: {run.get('status')}). The runner may be wedged: {run.get('html_url')}"
    )


# ---------------------------------------------------------------------------
# Contract: fetch is defined above (threads joined with body findings). resolve
# is re-exported from github_threads — a body finding has no thread to resolve,
# so only thread findings reach it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI shim — lets the provider be invoked directly. The skill's SKILL.md
# drives the provider through provider_loader, not this module.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="GitHub Action PR review provider (direct)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("wait", "fetch"):
        p = sub.add_parser(name)
        p.add_argument("pr_url")
        p.set_defaults(func=globals()[name])
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("thread_id")
    p_resolve.set_defaults(func=resolve)

    args = parser.parse_args()
    try:
        if args.command in ("wait", "fetch"):
            print(json.dumps(args.func(args.pr_url), indent=2))
        else:
            print(json.dumps(args.func(args.thread_id), indent=2))
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or "").strip() or str(e)
        print(f"ERROR ({args.command}): {msg}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR ({args.command}): {e}", file=sys.stderr)
        sys.exit(1)
