#!/usr/bin/env python3
"""Journals a session's wake at UserPromptSubmit, when the prompt is a memento handoff.

A handoff's last line names its file, and a wake naming that file is the link `journal recall`
follows from a session to the one that closed out before it. The event takes no matcher, so this
runs on every prompt, and a prompt that is not a handoff costs one log line. It never writes to
stdout, which Claude Code would add to the session's context, and never exits 2, the one status
that refuses a prompt: a wake that cannot be recorded is logged and re-raised, which exits 1 and
leaves the prompt as it was.
"""

import os
import re
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "lib"))
from ceiling_log import hook_payload, log, reason_of  # noqa: E402
from journal import JOURNAL, origin, record, woke  # noqa: E402

# The first line `finalize-session` frames a handoff with, and the opening of its last.
# [LAW:one-source-of-truth] The launcher writes them and this reads them, so this hook's suite and
# the launcher's each give it a handoff the launcher made, and a change to either side fails there.
OPENING = "=== Below is a message from an agent on behalf of the user ===\n"
NAMING = "This handoff verbatim on disk: "
# How Claude Code 2.1.299 delivers a paste. An initial prompt, and a paste on 2.1.197, arrive bare.
PASTED = re.compile(r'\s*<pasted_content id="([^"]*)">\n(.*)\n</pasted_content id="\1">\s*',
                    re.DOTALL)


def handoff_named(prompt):
    """The path of the handoff file a prompt is, or None for a prompt that is not a handoff.

    [LAW:parse-dont-validate] A handoff is the whole of its prompt, bare or pasted: the first line
    is the launcher's and the last names the file. A prompt that quotes one among other text is
    not a handoff, since the session it reaches did not wake from it."""
    pasted = PASTED.fullmatch(prompt)
    text = (pasted[2] if pasted else prompt).strip()
    last = text.rpartition("\n")[2]
    return last[len(NAMING):] if text.startswith(OPENING) and last.startswith(NAMING) else None


hook = None
try:
    hook = hook_payload("journal-wake", "UserPromptSubmit")
    handoff = handoff_named(hook["prompt"])
    verdict = "not a handoff"
    if handoff is not None:
        # The path is recorded as the handoff gives it: a wake finds its close-out by this text.
        # [LAW:single-enforcer] Whether it is a path the journal keeps is `woke`'s to say.
        record(woke(origin(hook["session_id"], hook["cwd"]), handoff), JOURNAL)
        verdict = f"woke from {handoff}"
except (Exception, SystemExit) as unresolved:
    log(hook if isinstance(hook, dict) else {}, f"stopped: {reason_of(unresolved)}")
    raise
log(hook, verdict)
