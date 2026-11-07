#!/usr/bin/env python3
"""Stub PR review provider for a locally run review agent.

Each contract operation raises NotImplementedError until the agent is wired in. The interface is
specified in PROVIDER_CONTRACT.md.
"""

from __future__ import annotations

from ports import Ports


class Provider:
    CAPABILITIES = {
        "resolve":        False,  # local agent findings have no GitHub thread to resolve
        "trigger":        True,   # local agent must be explicitly invoked
        "setup_check":    True,   # can check whether the agent binary is available
        "dismiss_review": False,  # no formal GitHub review to dismiss
    }

    def __init__(self, ports: Ports):
        self._ports = ports

    def setup_check(self, owner: str, repo: str) -> dict:
        raise NotImplementedError(
            "local_provider.setup_check is not yet implemented. "
            "Add a check that verifies the local review agent binary is on PATH "
            "and any required model/API credentials are configured."
        )

    def trigger(self, pr_url: str) -> dict:
        raise NotImplementedError(
            "local_provider.trigger is not yet implemented. "
            "Invoke the local review agent on the PR diff and return "
            '{"triggered": True} when the agent has been successfully started.'
        )

    def wait(self, pr_url: str) -> dict:
        raise NotImplementedError(
            "local_provider.wait is not yet implemented. "
            "If the local agent runs synchronously inside trigger(), implement wait "
            "as a no-op returning {\"status\": \"completed\", \"conclusion\": \"success\", "
            '"sha\": \"\", \"url\": None}. If it runs asynchronously, poll for completion.'
        )

    def fetch(self, pr_url: str) -> dict:
        raise NotImplementedError(
            "local_provider.fetch is not yet implemented. "
            "Run the local review agent on the PR and return its findings in "
            "canonical form: {\"findings\": [{...}]}. See PROVIDER_CONTRACT.md "
            "for the Finding schema."
        )
