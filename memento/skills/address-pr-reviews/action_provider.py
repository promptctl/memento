#!/usr/bin/env python3
"""GitHub Action PR review provider — implements the provider contract for the
copirate-code-review-agent GitHub Action reviewer.

The reviewer is a GitHub Action (promptctl/copirate-code-review-agent) that
runs on pull_request (opened, synchronize) and posts a formal PR review with
inline comments — i.e. ordinary resolvable review threads, authored by
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

Whether the head was REVIEWED is a separate fact from whether the run
completed: the action exits 0 on a spent round cap by design (a cost control
must not red a required check) and says so on the PR instead, as a review
ending with a not-reviewed marker. `wait` reads that artifact, so a capped
push comes back `reviewed: False` rather than as a clean run with zero
findings. [LAW:no-silent-failure]
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Optional

# [LAW:one-source-of-truth] thread fetch, Finding shape, and verified resolve
# are the shared GitHub primitives — imported, never copied. Import resolution
# is owned by provider_loader (loaded path) or script-mode sys.path (direct).
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
# What the reviewer left on the PR
# ---------------------------------------------------------------------------

# [LAW:one-source-of-truth] The reviewer's own marker grammar, mirrored from
# copirate-code-review-agent src/transport.js (REVIEW_MARKER and
# NOT_REVIEWED_MARKER_PREFIX). Every artifact the action leaves on a PR ends
# with exactly one of these: a completed round with the review marker, a run
# that reviewed NOTHING with a not-reviewed marker naming why. They are
# disjoint by construction — a reason is `[a-z-]+`, so no reason can splice
# the review marker back onto the end — which is why the ENDING is matched,
# never a loose `in`: a human review quoting a marker mid-body is nobody's
# artifact.
REVIEW_MARKER = "<!-- copirate-code-review-agent -->"
NOT_REVIEWED_MARKER_RE = re.compile(
    r"<!-- copirate-code-review-agent:not-reviewed:([a-z-]+) -->$"
)
# The provider's own reason for a head no artifact vouches for: the run
# completed, and the newest thing the reviewer left on the PR is a review of
# some OTHER commit, or nothing at all. The reviewer's reasons (`round-cap`,
# `fork`) pass through verbatim from the marker.
NO_REVIEW_FOR_HEAD = "no-review-for-head"


def parse_agent_artifact(body: str) -> Optional[dict]:
    """[LAW:parse-dont-validate] The one reader that turns a review body into
    what the reviewer left there — `{"kind": "review"}` or `{"kind":
    "not-reviewed", "reason": ...}` — or None for a body it did not write."""
    body = body.rstrip()
    if body.endswith(REVIEW_MARKER):
        return {"kind": "review"}
    m = NOT_REVIEWED_MARKER_RE.search(body)
    return {"kind": "not-reviewed", "reason": m.group(1)} if m else None


def head_review_verdict(reviews: list[dict], sha: str) -> dict:
    """[LAW:effects-at-boundaries] Pure. Was `sha` reviewed, judged from the
    reviewer's reviews on the PR (the `bot_reviews` shape)?

    "Newest" is the HIGHEST review id, never list order — the same rule the
    reviewer de-duplicates its own notice by: a second push while still capped
    posts NOTHING new, because the newest artifact already says so. A reader
    keyed on "a notice on the head SHA" would read that push as reviewed. So
    the head is reviewed exactly when the newest artifact is a review OF the
    head. A newer review of an older commit, or no artifact at all, is a run
    that completed without reviewing this commit — `no-review-for-head`, not a
    clean pass. [LAW:no-silent-failure]
    """
    artifacts = [
        (r["review_id"], r["commit_id"], artifact)
        for r in reviews
        if (artifact := parse_agent_artifact(r["body"])) is not None
    ]
    newest = max(artifacts, key=lambda a: a[0], default=None)
    if newest is None:
        return {"reviewed": False, "not_reviewed_reason": NO_REVIEW_FOR_HEAD}
    _, commit_id, artifact = newest
    reviewed = artifact["kind"] == "review" and commit_id == sha
    reason = None if reviewed else artifact.get("reason", NO_REVIEW_FOR_HEAD)
    return {"reviewed": reviewed, "not_reviewed_reason": reason}


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
# so the strings are matched literally and the source is cited, the same way
# REVIEW_MARKER above is. "### Changed files NOT reviewed" is deliberately NOT
# here: it is a coverage report, not a finding, and the head-review verdict already
# owns coverage. [LAW:no-silent-failure]
BODY_FINDING_HEADINGS = frozenset({
    "### Findings outside the reviewed diff",
    "### Findings the host would not post inline",
})

# [LAW:parse-dont-validate] One item line as the producer wrote it:
# `- ` + codeSpan(`path:line`) + ` — ` + severity-tagged body (transport.js
# renderFindingSection). The fence is one-or-more backticks with an optional pad
# space when the path itself contains a backtick, so `+ and the optional spaces
# mirror codeSpan exactly. A line that does not match is not silently dropped
# (that would recreate the very silent loss this parser exists to end) — it is
# surfaced with a null anchor below.
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
    blocking reviews (the `bot_reviews` shape). Scoped to CHANGES_REQUESTED by the
    same predicate `change_requests` uses, so the reviews whose bodies carry these
    findings are exactly the reviews step 8 dismisses — one predicate, and the
    finding set and the dismiss set cannot disagree. A dismissed review has left
    that state, so its body findings are disposed and no longer read.
    [LAW:one-source-of-truth]"""
    return [
        f
        for r in reviews if r.get("state") == "CHANGES_REQUESTED"
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
                **head_review_verdict(github_threads.bot_reviews(pr_url), sha),
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
