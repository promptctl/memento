#!/usr/bin/env python3
"""A session reset in place by `/clear` drops the ceiling override it set for itself.

The session ceiling is scoped to a session, and a session ends at a reset: a kill-and-relaunch
gets a new session id and so a new, empty override directory by construction, but `/clear` keeps
the id, so the file `ceiling set session` wrote would outlive the context that asked for it and
gate the successor on a number nobody set for it. SessionStart fires on `/clear` with
source="clear", distinct from a compaction's source="compact", and that is the one event this
runs on: the successor starts under the user and project layers until it sets its own.

Only the override is dropped. The ceiling in force is a live read of the layers at every stop,
so there is nothing else a reset could have left behind to re-derive. [LAW:one-source-of-truth]
"""

import contextlib
import os
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "lib"))
from ceiling_config import CONFIG_NAME, session_directory  # noqa: E402
from ceiling_log import hook_payload, log  # noqa: E402

# [LAW:no-silent-failure] the same guard the Stop hook keeps: every way this can stop before it
# acts - stdin that is not a JSON object, a hooks.json drifted onto another event or source, a
# payload missing session_id - exits nonzero with the reason on stderr, and first records the stop
# in the log, where a session whose override was never dropped is otherwise indistinguishable from
# one that never set one.
hook = None
override = "unresolved"
try:
    hook = hook_payload("clear-session-ceiling", "SessionStart")
    # A compaction is a SessionStart too, and the context after it is the same session still
    # running: its override must stand. The matcher in hooks.json keeps this off it, and this is
    # what makes a drifted matcher loud instead of an override silently dropped mid-session.
    if hook.get("source") != "clear":
        sys.exit(f"memento clear-session-ceiling: registered on SessionStart for source clear, "
                 f"called with source {hook.get('source')!r}. Fix hooks.json.")
    directory = session_directory(hook["session_id"])
    override = directory / CONFIG_NAME
    # Removing a file that is not there is the outcome asked for, not a failure: most sessions
    # never set an override. What is reported is whether one was dropped, so the log tells them
    # apart. [LAW:nothing-unseen] The override is the only file a session directory holds, so the
    # directory goes with it and a /clear leaves nothing of the session behind; a directory that
    # is not there, or that something else has since put a file in, is left as it is.
    dropped = override.exists()
    override.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        directory.rmdir()
except (Exception, SystemExit) as unresolved:
    reason = (unresolved.code if isinstance(unresolved, SystemExit)
              else f"{type(unresolved).__name__}: {unresolved}")
    log(hook if isinstance(hook, dict) else {}, f"stopped: {reason}", override=override)
    raise
log(hook, "dropped" if dropped else "no-override", override=override)
