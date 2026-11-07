#!/usr/bin/env python3
"""GitHub review-thread primitives shared by providers whose findings are PR review threads.

The GraphQL thread read, the canonical finding shape, and the verified resolve and reply mutations
are defined here once, so providers that post review threads use them instead of keeping copies.
Every gh call goes through a `GitHub` client built around one `Runner`. An entry point gets the
real client from `ports.production_ports()`; a test builds it over a fake Runner.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from subprocess import CompletedProcess
from typing import Callable, Sequence, Union

# The port to the gh binary: a finished gh is its CompletedProcess, and a gh that could not be
# started or did not finish is the OSError, so every outcome is one value the client decides from.
Outcome = Union[CompletedProcess[str], OSError]
Runner = Callable[[Sequence[str]], Outcome]

# How long one gh call may run. A gh stalled on a dead connection never exits, and a call that never
# returns is a failure no caller's deadline can see. A healthy call takes seconds.
GH_TIMEOUT_S = 60


def run_gh(args: Sequence[str]) -> Outcome:
    """The real Runner: runs `gh` with `args`, capturing stdout and stderr, and kills it after
    GH_TIMEOUT_S."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return TimeoutError(
            f"gh did not finish within {GH_TIMEOUT_S}s and was killed; whether it took effect is unknown"
        )
    except OSError as e:
        return e


class GhError(RuntimeError):
    """A `gh` call that failed. The message names the command and says why."""

    def __init__(self, command: str, reason: str):
        super().__init__(f"gh {command} {reason}")


# Flags whose value follows them. A value can be a whole query or reply body, which would bury the
# stderr that says why the call failed, so a failed command is named by the words before the first of
# these: the subcommand, the endpoint, and any bare flags among them.
_VALUE_FLAGS = frozenset({"-f", "--raw-field", "-F", "--field", "--input", "-q", "--jq"})


def _response_errors(stdout: str) -> dict:
    """Returns the `message` and `errors` fields of the last API response gh printed, and none of its
    data.

    They say why GitHub refused, which gh's one-line stderr does not. gh prints that response last:
    after the pages a `--paginate` read before failing, and alongside the partial `data` of a GraphQL
    response with errors, which can run to kilobytes.
    """
    lines = stdout.strip().splitlines()
    try:
        response = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        return {}
    if not isinstance(response, dict):
        return {}
    return {k: response[k] for k in ("message", "errors") if k in response}


def parse_pr(url: str) -> tuple[str, str, int]:
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"Not a PR URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


_THREADS_QUERY = (
    "query($owner:String!,$repo:String!,$num:Int!,$cursor:String){"
    "  repository(owner:$owner,name:$repo){"
    "    pullRequest(number:$num){"
    "      reviewThreads(first:100,after:$cursor){"
    "        pageInfo{ hasNextPage endCursor }"
    "        nodes{ id isResolved path originalStartLine originalLine startDiffSide diffSide"
    "          comments(first:100){"
    "            pageInfo{ hasNextPage endCursor }"
    "            nodes{ author{login} body originalCommit{oid} diffHunk } } } } } } }"
)

_COMMENTS_QUERY = (
    "query($id:ID!,$cursor:String){"
    "  node(id:$id){ ... on PullRequestReviewThread {"
    "    comments(first:100,after:$cursor){"
    "      pageInfo{ hasNextPage endCursor }"
    "      nodes{ author{login} body } } } } }"
)


_RESOLVE_MUTATION = (
    "mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{ isResolved } } }"
)

_REPLY_MUTATION = (
    "mutation($id:ID!,$body:String!){"
    "  addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id,body:$body}){"
    "    comment{ id } } }"
)


def _response_path(data: dict, *path: str, subject: str) -> dict:
    """Returns the value at a response path, or raises naming the field that is null.

    GraphQL returns HTTP 200 with nulls for inaccessible objects. Without this check the failure
    would surface as a TypeError far from its cause. A null on the path is an error, never an empty
    result.
    """
    node: object = data
    for depth, field in enumerate(path):
        child = node.get(field) if isinstance(node, dict) else None
        if child is None:
            raise RuntimeError(
                f"GraphQL response for {subject} has no "
                f"{'.'.join(path[:depth + 1])} (querying {'.'.join(path)}) — "
                "that object is missing or inaccessible, not empty."
            )
        node = child
    return node


def _next_cursor(block: dict, *, subject: str) -> str | None:
    """Returns the cursor for the next page, or None if this page is the last.

    `hasNextPage` with no `endCursor` raises. Returning None would present a partial set as complete,
    and reusing a null cursor would re-read the first page forever.
    """
    page_info = block["pageInfo"]
    if not page_info["hasNextPage"]:
        return None
    cursor = page_info.get("endCursor")
    if not cursor:
        raise RuntimeError(
            f"GraphQL pagination for {subject} promised another page "
            f"(hasNextPage true) but named no endCursor (got {cursor!r}). "
            "The next page cannot be requested and the set read so far is "
            "incomplete — do not treat it as the whole set."
        )
    return cursor


def _line_at_commit(side: str, line: int | None) -> int | None:
    """Returns a thread end's line as a line at its comment's commit: the line on the RIGHT side,
    None on the LEFT. Raises ValueError for a side GitHub does not define."""
    if side not in ("LEFT", "RIGHT"):
        raise ValueError(f"review thread diff side {side!r} is neither LEFT nor RIGHT")
    return line if side == "RIGHT" else None


def _build_findings(threads: list[dict]) -> list[dict]:
    """Builds canonical findings from threads. Reviewer and human threads share one shape.

    A thread's finding is its first comment, so its `commit_id` is the commit that comment was made
    on. GitHub names none for a commit it no longer has, and a thread with no comments has none. Its
    lines come from `originalStartLine`..`originalLine`, rows in that comment's diff: the thread's `line` is
    GitHub's mapping onto the latest commit, null once the thread is outdated, so it would name a line
    from another commit. GitHub gives a single-line comment no start line or start side; its range is
    its one line.

    Each end is a row on one side of the diff the comment was made on. A RIGHT row is a line in the
    file at `commit_id`. A LEFT row is a deleted line, in a file GitHub names no commit for — the old
    side is the merge base, a parent, or a range start depending on the view the comment was left
    in — so that end is null, and `diff_hunk`, the text GitHub recorded, shows the deleted code.
    """
    findings = []
    for t in threads:
        nodes = t.get("comments", {}).get("nodes") or []
        first = nodes[0] if nodes else {}
        end_side = t["diffSide"]
        end = _line_at_commit(end_side, t.get("originalLine"))
        start = _line_at_commit(t.get("startDiffSide") or end_side,
                                t.get("originalStartLine") or t.get("originalLine"))
        findings.append({
            "file":            t.get("path"),
            "line_start":      start,
            "line_end":        end,
            "diff_hunk":       first.get("diffHunk"),
            "body":            first.get("body", ""),
            "author":          (first.get("author") or {}).get("login"),
            "commit_id":       (first.get("originalCommit") or {}).get("oid"),
            "thread_id":       t["id"],
            "is_resolved":     t.get("isResolved", False),
            "thread_comments": [
                {"author": (c.get("author") or {}).get("login"), "body": c.get("body", "")}
                for c in nodes
            ],
        })
    return findings


# Selects the automated reviewer's reviews. Its reviews have `.user.type` "Bot" and people's are "User".
_BOT_REVIEWS_JQ = (
    '.[] | select(.user.type=="Bot")'
    ' | {review_id: .id, author: .user.login, commit_id, state, body}'
)


def is_blocking_review(review: dict) -> bool:
    """Returns True if an automated-reviewer review blocks the PR, given a `bot_reviews` entry.

    `change_requests` and the action provider's body findings both use this predicate. Each calls
    `bot_reviews` for its own read; the reviewer changes reviews only on a re-review after a push,
    so two reads with no push between them see the same reviews.
    """
    return review["state"] == "CHANGES_REQUESTED"


class GitHub:
    """Every gh operation, run through the one Runner the client was built with."""

    def __init__(self, run: Runner):
        self._run = run

    def gh(self, *args: str) -> str:
        """Runs `gh` with the given arguments and returns its stripped stdout. Raises GhError if it
        fails."""
        command = " ".join(itertools.takewhile(lambda a: a not in _VALUE_FLAGS, args))
        result = self._run(args)
        if isinstance(result, OSError):
            raise GhError(command, f"failed to run: {result}") from result
        if result.returncode == 0:
            return result.stdout.strip()
        reason = f"exited {result.returncode}: {result.stderr.strip() or '(no stderr)'}"
        errors = _response_errors(result.stdout)
        raise GhError(command, f"{reason} Response: {errors}" if errors else reason)

    def head_sha(self, owner: str, repo: str, pr_num: int) -> str:
        return self.gh("api", f"repos/{owner}/{repo}/pulls/{pr_num}", "--jq", ".head.sha")

    def paginated(self, endpoint: str, jq: str) -> list[dict]:
        """Returns the objects `jq` emits across every page of a REST list endpoint.

        `gh --paginate` applies `--jq` per page and concatenates the output, so the filter must emit
        one object per line. A filter that wraps each page in an array does not parse as one
        document, and `--slurp` cannot be combined with `--jq`.
        """
        out = self.gh("api", "--paginate", endpoint, "--jq", jq)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def _graphql(self, query: str, **variables) -> dict:
        """Runs one GraphQL query or mutation; every GraphQL call in this module goes through here.

        Strings go through `-f`, which sends them raw, and ints through `-F`. `-F` would type-infer
        an all-digit cursor or id into a number. A `None` variable is omitted, so GraphQL reads it as
        null rather than as the empty string, which is not a valid cursor.
        """
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                continue
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        out = self.gh(*args)
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            raise RuntimeError(f"gh api graphql printed {out!r}, not a JSON object")
        return data

    def _page_of_threads(self, owner: str, repo: str, pr_num: int, cursor: str | None) -> dict:
        data = self._graphql(_THREADS_QUERY, owner=owner, repo=repo, num=pr_num, cursor=cursor)
        return _response_path(
            data, "data", "repository", "pullRequest", "reviewThreads",
            subject=f"{owner}/{repo}#{pr_num} review threads",
        )

    def _complete_comments(self, thread: dict) -> None:
        """Appends one thread's remaining comment pages to its first page.

        This loop is written by hand because `gh --paginate` walks only one top-level connection,
        and each thread's comments are a nested connection.
        """
        subject = f"comments of review thread {thread['id']}"
        block = thread["comments"]
        cursor = _next_cursor(block, subject=subject)
        while cursor is not None:
            data = self._graphql(_COMMENTS_QUERY, id=thread["id"], cursor=cursor)
            block = _response_path(data, "data", "node", "comments", subject=subject)
            thread["comments"]["nodes"].extend(block["nodes"])
            cursor = _next_cursor(block, subject=subject)

    def _fetch_threads(self, owner: str, repo: str, pr_num: int) -> list[dict]:
        """Returns every review thread on the PR, with all of its comments.

        Both loops run until `hasNextPage` is false, so the result is the whole set or the call
        raises.
        """
        threads: list[dict] = []
        cursor: str | None = None
        while True:
            block = self._page_of_threads(owner, repo, pr_num, cursor)
            threads.extend(block["nodes"])
            cursor = _next_cursor(block, subject=f"{owner}/{repo}#{pr_num} review threads")
            if cursor is None:
                break
        for thread in threads:
            self._complete_comments(thread)
        return threads

    def fetch(self, pr_url: str) -> dict:
        """Return all review threads as canonical findings."""
        owner, repo, pr_num = parse_pr(pr_url)
        return {"findings": _build_findings(self._fetch_threads(owner, repo, pr_num))}

    def resolve(self, thread_id: str) -> dict:
        """Resolves one review thread and returns only after GitHub confirms it. Raises RuntimeError
        if it does not."""
        data = self._graphql(_RESOLVE_MUTATION, id=thread_id)
        # A false, a null, and a missing payload are all one outcome: no confirmation.
        payload = (data.get("data") or {}).get("resolveReviewThread") or {}
        if (payload.get("thread") or {}).get("isResolved") is not True:
            raise RuntimeError(
                f"resolveReviewThread did not confirm resolution for {thread_id}. "
                f"The thread is NOT resolved — do not move on. Response: {data!r}"
            )
        return {"thread_id": thread_id, "is_resolved": True}

    def reply(self, thread_id: str, body: str) -> dict:
        """Posts a reply on one review thread and returns only after GitHub names the created
        comment. Raises RuntimeError if it does not."""
        data = self._graphql(_REPLY_MUTATION, id=thread_id, body=body)
        payload = (data.get("data") or {}).get("addPullRequestReviewThreadReply") or {}
        comment_id = (payload.get("comment") or {}).get("id")
        if not comment_id:
            raise RuntimeError(
                f"addPullRequestReviewThreadReply returned no comment id for {thread_id}. "
                f"The reply was NOT posted. Response: {data!r}"
            )
        return {"thread_id": thread_id, "comment_id": comment_id}

    def bot_reviews(self, pr_url: str) -> list[dict]:
        """Returns every automated-reviewer review on the PR, oldest first, as
        `{review_id, author, commit_id, state, body}` entries.

        Reads every page, since a PR with many rounds can exceed one page and a single-page read
        would drop the newest review.
        """
        owner, repo, pr_num = parse_pr(pr_url)
        return self.paginated(f"repos/{owner}/{repo}/pulls/{pr_num}/reviews", _BOT_REVIEWS_JQ)

    def change_requests(self, pr_url: str) -> dict:
        """Returns the automated reviewer's CHANGES_REQUESTED reviews that this round must dismiss.

        Human reviews are never included, so a human's request is cleared only by that human. The
        result is read when findings are fetched and dismissed by id afterwards; reviews created by
        a later push are not in it.
        """
        return {"reviews": [
            {"review_id": r["review_id"], "author": r["author"], "commit_id": r["commit_id"]}
            for r in self.bot_reviews(pr_url) if is_blocking_review(r)
        ]}

    def dismiss_review(self, pr_url: str, review_id: int, message: str) -> dict:
        """Dismisses one review with a message and returns only after GitHub confirms the dismissal.

        Raises RuntimeError if the review is not DISMISSED afterwards."""
        owner, repo, pr_num = parse_pr(pr_url)
        state = self.gh(
            "api", "--method", "PUT",
            f"repos/{owner}/{repo}/pulls/{pr_num}/reviews/{review_id}/dismissals",
            "-f", f"message={message}",
            "-f", "event=DISMISS",
            "--jq", ".state",
        )
        if state != "DISMISSED":
            raise RuntimeError(
                f"Dismissing review {review_id} was not confirmed (state={state!r}). "
                "The review still blocks the PR — do not treat the round as closed."
            )
        return {"review_id": review_id, "is_dismissed": True}
