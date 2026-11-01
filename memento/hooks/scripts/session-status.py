#!/usr/bin/env python3
"""Publishes the session's ceiling status to cc-candybar's bar at SessionStart.

Runs for the `startup` and `resume` sources. The Stop hook writes the status only at a stop, so
without this the bar's ceiling segment stays hidden until the first turn ends. `clear` is handled
by clear-session-ceiling.py, and `compact` leaves the status unchanged. A failed publish is logged
and re-raised.
"""

import os
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "lib"))
from candybar import publish  # noqa: E402
from ceiling_config import anchored, session_directory  # noqa: E402
from ceiling_log import hook_payload, log, reason_of  # noqa: E402

hook = None
try:
    hook = hook_payload("session-status", "SessionStart")
    facts = publish(session_directory(hook["session_id"]), anchored(hook["cwd"]))
except (Exception, SystemExit) as unresolved:
    log(hook if isinstance(hook, dict) else {}, f"stopped: {reason_of(unresolved)}")
    raise
log(hook, "published", **facts)
