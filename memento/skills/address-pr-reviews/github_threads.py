#!/usr/bin/env python3
"""GitHub review-thread primitives shared by providers whose findings are PR review threads.

The GraphQL thread read, the canonical finding shape, and the verified resolve mutation are defined
here once, so providers that post review threads import them instead of keeping copies.
"""

from __future__ import annotations

import json
import re
import subprocess


def gh(*args: str) -> str:
    """Runs `gh` with the given arguments and returns its stripped stdout."""
    return subprocess.check_output(
        ["gh", *args], text=True, stderr=subprocess.PIPE
    ).strip()


def parse_pr(url: str) -> tuple[str, str, int]:
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"Not a PR URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def head_sha(owner: str, repo: str, pr_num: int) -> str:
    return gh("api", f"repos/{owner}/{repo}/pulls/{pr_num}", "--jq", ".head.sha")


def paginated(endpoint: str, jq: str) -> list[dict]:
    """Returns the objects `jq` emits across every page of a REST list endpoint.

    `gh --paginate` applies `--jq` per page and concatenates the output, so the filter must emit one
    object per line. A filter that wraps each page in an array does not parse as one document, and
    `--slurp` cannot be combined with `--jq`.
    """
    out = gh("api", "--paginate", endpoint, "--jq", jq)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


_THREADS_QUERY = (
    "query($owner:String!,$repo:String!,$num:Int!,$cursor:String){"
    "  repository(owner:$owner,name:$repo){"
    "    pullRequest(number:$num){"
    "      reviewThreads(first:100,after:$cursor){"
    "        pageInfo{ hasNextPage endCursor }"
    "        nodes{ id isResolved path line"
    "          comments(first:100){"
    "            pageInfo{ hasNextPage endCursor }"
    "            nodes{ author{login} body } } } } } } }"
)

_COMMENTS_QUERY = (
    "query($id:ID!,$cursor:String){"
    "  node(id:$id){ ... on PullRequestReviewThread {"
    "    comments(first:100,after:$cursor){"
    "      pageInfo{ hasNextPage endCursor }"
    "      nodes{ author{login} body } } } } }"
)


def _graphql(query: str, **variables) -> dict:
    """Runs one GraphQL query.

    Strings go through `-f`, which sends them raw, and ints through `-F`. `-F` would type-infer an
    all-digit cursor or id into a number. A `None` variable is omitted, so GraphQL reads it as null
    rather than as the empty string, which is not a valid cursor.
    """
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    return json.loads(gh(*args))


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


def _page_of_threads(owner: str, repo: str, pr_num: int, cursor: str | None) -> dict:
    data = _graphql(_THREADS_QUERY, owner=owner, repo=repo, num=pr_num, cursor=cursor)
    return _response_path(
        data, "data", "repository", "pullRequest", "reviewThreads",
        subject=f"{owner}/{repo}#{pr_num} review threads",
    )


def _complete_comments(thread: dict) -> None:
    """Appends one thread's remaining comment pages to its first page.

    This loop is written by hand because `gh --paginate` walks only one top-level connection, and
    each thread's comments are a nested connection.
    """
    subject = f"comments of review thread {thread['id']}"
    block = thread["comments"]
    cursor = _next_cursor(block, subject=subject)
    while cursor is not None:
        data = _graphql(_COMMENTS_QUERY, id=thread["id"], cursor=cursor)
        block = _response_path(data, "data", "node", "comments", subject=subject)
        thread["comments"]["nodes"].extend(block["nodes"])
        cursor = _next_cursor(block, subject=subject)


def _fetch_threads(owner: str, repo: str, pr_num: int) -> list[dict]:
    """Returns every review thread on the PR, with all of its comments.

    Both loops run until `hasNextPage` is false, so the result is the whole set or the call raises.
    """
    threads: list[dict] = []
    cursor: str | None = None
    while True:
        block = _page_of_threads(owner, repo, pr_num, cursor)
        threads.extend(block["nodes"])
        cursor = _next_cursor(block, subject=f"{owner}/{repo}#{pr_num} review threads")
        if cursor is None:
            break
    for thread in threads:
        _complete_comments(thread)
    return threads


def _build_findings(threads: list[dict]) -> list[dict]:
    """Builds canonical findings from threads. Reviewer and human threads share one shape."""
    findings = []
    for t in threads:
        nodes = t.get("comments", {}).get("nodes") or []
        first = nodes[0] if nodes else {}
        findings.append({
            "file":            t.get("path"),
            "line_start":      t.get("line"),
            "line_end":        t.get("line"),
            "body":            first.get("body", ""),
            "author":          (first.get("author") or {}).get("login"),
            "thread_id":       t["id"],
            "is_resolved":     t.get("isResolved", False),
            "thread_comments": [
                {"author": (c.get("author") or {}).get("login"), "body": c.get("body", "")}
                for c in nodes
            ],
        })
    return findings


def fetch(pr_url: str) -> dict:
    """Return all review threads as canonical findings."""
    owner, repo, pr_num = parse_pr(pr_url)
    threads = _fetch_threads(owner, repo, pr_num)
    return {"findings": _build_findings(threads)}


def resolve(thread_id: str) -> dict:
    """Resolves one review thread and returns only after GitHub confirms it. Raises RuntimeError if it
    does not."""
    confirmed = gh(
        "api", "graphql",
        "-f", "query=mutation($id:ID!){resolveReviewThread(input:{threadId:$id})"
              "{thread{isResolved}}}",
        "-F", f"id={thread_id}",
        "--jq", ".data.resolveReviewThread.thread.isResolved",
    )
    if confirmed != "true":
        raise RuntimeError(
            f"resolveReviewThread did not confirm resolution for {thread_id} "
            f"(got {confirmed!r}). The thread is NOT resolved — do not move on."
        )
    return {"thread_id": thread_id, "is_resolved": True}


# Selects the automated reviewer's reviews. Its reviews have `.user.type` "Bot" and people's are "User".
_BOT_REVIEWS_JQ = (
    '.[] | select(.user.type=="Bot")'
    ' | {review_id: .id, author: .user.login, commit_id, state, body}'
)


def bot_reviews(pr_url: str) -> list[dict]:
    """Returns every automated-reviewer review on the PR, oldest first, as
    `{review_id, author, commit_id, state, body}` entries.

    Reads every page, since a PR with many rounds can exceed one page and a single-page read would
    drop the newest review.
    """
    owner, repo, pr_num = parse_pr(pr_url)
    return paginated(f"repos/{owner}/{repo}/pulls/{pr_num}/reviews", _BOT_REVIEWS_JQ)


def is_blocking_review(review: dict) -> bool:
    """Returns True if an automated-reviewer review blocks the PR, given a `bot_reviews` entry.

    `change_requests` and `action_provider.body_findings` both use this predicate. Each calls
    `bot_reviews` for its own read; the reviewer changes reviews only on a re-review after a push,
    so two reads with no push between them see the same reviews.
    """
    return review["state"] == "CHANGES_REQUESTED"


def change_requests(pr_url: str) -> dict:
    """Returns the automated reviewer's CHANGES_REQUESTED reviews that this round must dismiss.

    Human reviews are never included, so a human's request is cleared only by that human. The result
    is read when findings are fetched and dismissed by id afterwards; reviews created by a later push
    are not in it.
    """
    return {"reviews": [
        {"review_id": r["review_id"], "author": r["author"], "commit_id": r["commit_id"]}
        for r in bot_reviews(pr_url) if is_blocking_review(r)
    ]}


def dismiss_review(pr_url: str, review_id: int, message: str) -> dict:
    """Dismisses one review with a message and returns only after GitHub confirms the dismissal.

    Raises RuntimeError if the review is not DISMISSED afterwards."""
    owner, repo, pr_num = parse_pr(pr_url)
    state = gh(
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
