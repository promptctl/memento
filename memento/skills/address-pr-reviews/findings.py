"""The findings a review round works from: a provider's findings, each placed against the PR's head.

A finding is a statement about the commit it was produced against, which may not be the commit the
agent is looking at. Every provider's findings are read through `fetch` here, so the rule that says
where a finding stands against the head is written once, for every provider. [LAW:single-enforcer]
"""

from __future__ import annotations

import re
from typing import Literal, Optional

# Where a finding's commit stands against the head: the head itself, an older commit it can be
# diffed against, or a commit GitHub no longer names.
AgainstHead = Literal["head", "older", "unknown"]


def against_head(commit_id: Optional[str], head_sha: str) -> AgainstHead:
    if commit_id is None:
        return "unknown"
    return "head" if commit_id == head_sha else "older"


def reviewed_sha(review: dict) -> str:
    """Returns the full SHA of the commit `review` covered, where `review` is the result a provider's
    `wait` returned. Raises ValueError for a review that did not succeed, whose findings are absent,
    not empty, or for a sha that is not a full SHA, which would compare unequal to the head on every
    round so the loop never ends. [LAW:parse-dont-validate]
    """
    if review["conclusion"] != "success":
        raise ValueError(f"the review concluded {review['conclusion']!r}, not success: "
                         f"its findings are absent, not empty. Run: {review['url']}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", review["sha"]):
        raise ValueError(f"the review's sha {review['sha']!r} is not a full commit SHA")
    return review["sha"]


def fetch(provider, pr_url: str, review: dict) -> dict:
    """Returns `{"head_sha", "done", "findings"}`: every finding the provider reports, resolved or
    not, each with `against_head` set, and whether the review loop is done: `review`, the result
    `wait` returned, succeeded on the commit the head is at, and no finding is unresolved.
    [LAW:single-enforcer] the done rule lives here, not in the prose that reads it.

    The head is read after the findings. A push between the two reads moves the head past those
    findings, so they read as older, and past the reviewed commit, so the loop reads as not done;
    neither reads as current once the head has moved. [LAW:no-ambient-temporal-coupling]
    """
    reviewed = reviewed_sha(review)
    found = provider.fetch(pr_url)["findings"]
    head = provider.head_sha(pr_url)
    return {
        "head_sha": head,
        "done": head == reviewed and all(f["is_resolved"] for f in found),
        "findings": [{**f, "against_head": against_head(f["commit_id"], head)} for f in found],
    }
