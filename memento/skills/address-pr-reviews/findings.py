"""The findings a review round works from: a provider's findings, each placed against the PR's head.

A finding is a statement about the commit it was produced against, which may not be the commit the
agent is looking at. Every provider's findings are read through `fetch` here, so the rule that says
where a finding stands against the head is written once, for every provider. [LAW:single-enforcer]
"""

from __future__ import annotations

from typing import Literal, Optional

# Where a finding's commit stands against the head: the head itself, an older commit it can be
# diffed against, or a commit GitHub no longer names.
AgainstHead = Literal["head", "older", "unknown"]


def against_head(commit_id: Optional[str], head_sha: str) -> AgainstHead:
    if commit_id is None:
        return "unknown"
    return "head" if commit_id == head_sha else "older"


def fetch(provider, pr_url: str, reviewed_sha: str) -> dict:
    """Returns `{"head_sha", "done", "findings"}`: every pending finding, each with `against_head`
    set, and whether the review loop is done: the head is the commit `reviewed_sha` names, the one
    the provider's `wait` returned once its review completed, and no finding is unresolved.
    [LAW:single-enforcer] the done rule lives here, not in the prose that reads it.

    The head is read after the findings. A push between the two reads moves the head past those
    findings, so they read as older, and past the reviewed commit, so the loop reads as not done;
    neither reads as current once the head has moved. [LAW:no-ambient-temporal-coupling]
    """
    found = provider.fetch(pr_url)["findings"]
    head = provider.head_sha(pr_url)
    return {
        "head_sha": head,
        "done": head == reviewed_sha and all(f["is_resolved"] for f in found),
        "findings": [{**f, "against_head": against_head(f["commit_id"], head)} for f in found],
    }
