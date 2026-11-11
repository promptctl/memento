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
DROPPED = f"[older lines dropped at {LOG_CAP} bytes]\n".encode()


def run(name, event, work, every_line=lambda hook: {}):
    """Runs one hook: reads its payload for `event`, does its `work`, and logs one line for the run.

    `work(hook, facts)` returns the run's verdict, and puts each log field into `facts` as it learns
    it, so a run that fails logs `stopped: <reason>` with the fields it reached and none it did not.
    `every_line(hook)` gives the fields every line of the hook carries whatever the outcome, read as
    the line is written, so a failure of its own is its to report as a field, never to raise. A
    payload that is no object, or is for another event, stops the run before the work: its line
    names the session and event the payload gave, and carries no `every_line` fields, since the run
    was never the hook's. [LAW:single-enforcer] A failure, an exit included, is re-raised once it is
    logged, so it stays loud: the hook ends as the failure would have ended it."""
    hook, facts, also = {}, {}, lambda hook: {}
    try:
        hook = _object(name)
        _for_event(name, hook, event)
        also = every_line
        verdict = work(hook, facts)
    except (Exception, SystemExit) as failure:
        _write(name, hook, f"stopped: {reason_of(failure)}", facts | also(hook))
        raise
    _write(name, hook, verdict, facts | also(hook))


def _object(name):
    """Reads the hook payload from stdin, exiting with a reason if it is not a JSON object."""
    hook = json.load(sys.stdin)
    if not isinstance(hook, dict):
        sys.exit(f"memento {name}: stdin must be a JSON object, got {type(hook).__name__}.")
    return hook


def _for_event(name, hook, event):
    """Exits with a reason if the payload's hook_event_name is not `event`."""
    if hook.get("hook_event_name") != event:
        sys.exit(f"memento {name}: registered on {event}, called with hook_event_name "
                 f"{hook.get('hook_event_name')!r}. Fix hooks.json.")


def reason_of(failure):
    """Formats a failure as one log field: a SystemExit's message, or the exception type and args."""
    return (failure.code if isinstance(failure, SystemExit)
            else f"{type(failure).__name__}: {failure}")


def _kept(tail):
    """What a log over the cap keeps, given its newest bytes: the `DROPPED` line, then the whole
    lines that end in `tail` after its first byte, none when no line ends there."""
    newline = tail.find(b"\n")
    return DROPPED + (tail[newline + 1:] if newline >= 0 else b"")


def _write(name, hook, verdict, fields):
    """Appends one line with the time, session, event, the `key=value` fields, and the verdict,
    first rewriting a log over the cap to what it `_kept`.

    A write failure is printed to stderr, naming the hook, and not raised, so logging cannot disable
    the hook."""
    fields = {"session": str(hook.get("session_id"))[:8], "event": hook.get("hook_event_name"),
              **fields}
    line = (f"{datetime.now().isoformat(timespec='seconds')} "
            f"{' '.join(f'{key}={value}' for key, value in fields.items())} -> {verdict}\n")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        # [LAW:no-ambient-temporal-coupling] every write is made under the lock, so the end of the
        # log is where the last holder left it; opened without O_APPEND so it can be rewritten in
        # place, never moved into place: a hook waiting on the lock holds this inode, and would
        # append to an unlinked file.
        with open(LOG_FILE, "r+b",
                  opener=lambda path, flags: os.open(path, flags | os.O_CREAT, 0o666)) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if handle.seek(0, os.SEEK_END) > LOG_CAP:
                # The rewritten log, its DROPPED line included, fits in half the cap, so the log is
                # rewritten once per half-cap of new lines, not on every line once it sits at the
                # cap. It is written over the log's start and the log is cut after: a hook killed
                # between the two leaves a log still over the cap, whose newest half the rewrite
                # cannot have reached, for the next write to keep whole.
                handle.seek(len(DROPPED) - LOG_CAP // 2 - 1, os.SEEK_END)
                rewritten = _kept(handle.read())
                handle.seek(0)
                handle.write(rewritten)
                handle.truncate()
            handle.write(line.encode(errors="backslashreplace"))
    except OSError as failure:
        print(f"memento {name}: cannot write {LOG_FILE}: {failure}", file=sys.stderr)
