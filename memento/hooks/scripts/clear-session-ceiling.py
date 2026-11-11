#!/usr/bin/env python3
"""Drops the session's ceiling override when `/clear` resets the session in place.

This runs on SessionStart with source="clear", since `/clear` keeps the session id and its override
file would otherwise outlive the context that set it. Compaction (source="compact") is not handled
here. Only the override is removed, because the ceiling is read from the config layers at every stop.
"""

import contextlib
import os
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "lib"))
from ceiling_config import CONFIG_NAME, session_directory  # noqa: E402
from ceiling_log import run  # noqa: E402
# Republishes the session's status so the bar shows the ceiling the successor runs under.
from candybar import published  # noqa: E402


def drop(hook, facts):
    """Drops the session's override and returns whether there was one to drop."""
    # A compaction also fires SessionStart and its override must stand, so any other source is an
    # error: a drifted hooks.json matcher should fail loudly, not drop an override mid-session.
    if hook.get("source") != "clear":
        sys.exit(f"memento clear-session-ceiling: registered on SessionStart for source clear, "
                 f"called with source {hook.get('source')!r}. Fix hooks.json.")
    directory = session_directory(hook["session_id"])
    override = facts["override"] = directory / CONFIG_NAME
    # A missing override is not a failure, since most sessions never set one; the log records
    # whether one was dropped. The session directory is removed too if nothing else is left in it.
    dropped = override.exists()
    override.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        directory.rmdir()
    return "dropped" if dropped else "no-override"


run("clear-session-ceiling", "SessionStart", drop, every_line=published)
