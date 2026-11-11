#!/usr/bin/env python3
"""Runs a hook and writes its line to the context-ceiling log: one line per run, in the same format
from every hook.

Every hook that logs runs through `run`, so a grep for a session matches lines from all of them and
a failure is logged the same way whichever hook it stops. The owner lives in this module because
each hook script runs its logic at import time and cannot be imported by another.
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


def run(name, event, work, every_line=lambda hook: {}):
    """Runs one hook: reads its payload for `event`, does its `work`, and logs one line for the run.

    `work(hook, facts)` returns the run's verdict, and puts each log field into `facts` as it learns
    it, so a run that fails logs `stopped: <reason>` with the fields it reached and none it did not.
    `every_line(hook)` gives the fields every line of the hook carries whatever the outcome, read as
    the line is written. [LAW:single-enforcer] A failure, an exit included, is re-raised once it is
    logged, so it stays loud: the hook exits 1 with its reason on stderr."""
    hook, facts = {}, {}
    try:
        hook = _payload(name, event)
        verdict = work(hook, facts)
    except (Exception, SystemExit) as failure:
        _write(name, hook, f"stopped: {reason_of(failure)}", facts | every_line(hook))
        raise
    _write(name, hook, verdict, facts | every_line(hook))


def _payload(name, event):
    """Reads the hook payload from stdin and checks that it is for the expected event.

    Exits with a reason if stdin is not a JSON object or if its hook_event_name is not `event`."""
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


def _write(name, hook, verdict, fields):
    """Appends one line with the time, session, event, the `key=value` fields, and the verdict.

    A write failure is printed to stderr, naming the hook, and not raised, so logging cannot disable
    the hook."""
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
        print(f"memento {name}: cannot write {LOG_FILE}: {failure}", file=sys.stderr)
