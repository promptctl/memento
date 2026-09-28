#!/usr/bin/env python3
"""The context-ceiling log: one line per hook run, in one shape, whichever hook wrote it.

[LAW:no-silent-failure] a hook that allows emits nothing, and so does one that never ran; the log
is the only place that difference exists. Two hooks write it - the Stop gate that decides on a
ceiling, and the SessionStart hook that drops a session's override on `/clear` - and a line one
of them wrote has to read like a line the other wrote, so a reader grepping for a session finds
both. [LAW:one-source-of-truth] The writer lives here, beside the config module both hooks
already import from, rather than in either hook: a hook reads stdin at import and cannot lend a
function to the other.
"""

import fcntl
import os
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(os.environ.get("MEMENTO_CEILING_LOG")
                or Path.home() / ".claude" / "memento" / "context-ceiling.log")
LOG_CAP = 2_000_000


def log(hook, verdict, **fields):
    """Append one line: when, which session and event, the fields the hook decided on, and the
    verdict. Its own failure is reported but not fatal - raising would take the gate down with the
    instrumentation. `fields` are the hook's own facts, `key=value` each, so a Stop line carries
    the count and the ceiling that won and a clear line carries the file it removed."""
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
