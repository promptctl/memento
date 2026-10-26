#!/usr/bin/env python3
"""A session that starts or resumes has its ceiling on cc-candybar's bar from its first frame.

The Stop hook writes the session's status at every stop, but a session that has not yet ended a
turn has had no stop, and the bar's ceiling segment stays hidden until a status exists. This writes
it at SessionStart, for the sources that begin a session here: `startup` and `resume`. `clear` has
its own hook, which drops the session's override before writing; `compact` changes nothing the
status says.

Publishing is this hook's whole job, so a failure to publish is its failure: logged, then raised,
as the clear hook does with its own. [LAW:no-silent-failure]
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
