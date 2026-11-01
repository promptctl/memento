#!/usr/bin/env python3
"""Writes the context-ceiling log: one line per hook run, in the same format from every hook.

The Stop gate and the SessionStart clear hook both write here, so a grep for a session matches
lines from either. The writer lives in this module because each hook script runs its logic at
import time and cannot be imported by the other.
"""

import fcntl
import json
import os
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(os.environ.get("MEMENTO_CEILING_LOG")
                or Path.home() / ".claude" / "memento" / "context-ceiling.log")
LOG_CAP = 2_000_000


def hook_payload(name, event):
    """Reads the hook payload from stdin and checks that it is for the expected event.

    Exits with a reason if stdin is not a JSON object or if its hook_event_name is not `event`.
    The caller logs that exit before re-raising it."""
    hook = json.load(sys.stdin)
    if not isinstance(hook, dict):
        sys.exit(f"memento {name}: stdin must be a JSON object, got {type(hook).__name__}.")
    if hook.get("hook_event_name") != event:
        sys.exit(f"memento {name}: registered on {event}, called with hook_event_name "
                 f"{hook.get('hook_event_name')!r}. Fix hooks.json.")
    return hook


def reason_of(failure):
    """Formats a failure as one log field: a SystemExit's message, or the exception type and args."""
    return (failure.code if isinstance(failure, SystemExit)
            else f"{type(failure).__name__}: {failure}")


def log(hook, verdict, **fields):
    """Appends one line with the time, session, event, the caller's `key=value` fields, and the verdict.

    A write failure is printed to stderr and not raised, so logging cannot disable the hook."""
    facts = " ".join(f"{key}={value}" for key, value in fields.items())
    line = (f"{datetime.now().isoformat(timespec='seconds')} "
            f"session={str(hook.get('session_id'))[:8]} event={hook.get('hook_event_name')} "
            f"{facts} -> {verdict}\n")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if os.fstat(handle.fileno()).st_size > LOG_CAP:
                handle.truncate(0)
                handle.write(f"[truncated at {LOG_CAP} bytes]\n")
            handle.write(line)
    except OSError as failure:
        print(f"memento context ceiling: cannot write {LOG_FILE}: {failure}", file=sys.stderr)
