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
from ceiling_log import run  # noqa: E402
from journal import JOURNAL, Refused, origin, record, woke  # noqa: E402

# The first line `finalize-session` frames a handoff with, and the opening of its last.
# [LAW:one-source-of-truth] The launcher writes them and this reads them, so this hook's suite and
# the launcher's each give it a handoff the launcher made, and a change to either side fails there.
OPENING = "=== Below is a message from an agent on behalf of the user ===\n"
NAMING = "This handoff verbatim on disk: "
# How Claude Code 2.1.299 delivers a paste. An initial prompt, and a paste on 2.1.197, arrive bare.
# This matches one paste from the start of a prompt: its text ends at the first closing tag with
# its id.
PASTED = re.compile(r'\s*<pasted_content id="([^"]*)">\n(.*?)\n</pasted_content id="\1">\s*',
                    re.DOTALL)


def handoff_named(prompt):
    """The path of the handoff file a prompt is, or None for a prompt that is not a handoff.

    [LAW:parse-dont-validate] A handoff is the whole of its prompt, bare or pasted: the first line
    is the launcher's and the last names the file. A prompt that quotes one among other text is
    not a handoff, since the session it reaches did not wake from it. A paste that ends before its
    prompt does is among other text too, a second paste included."""
    pasted = PASTED.match(prompt)
    text = (pasted[2] if pasted and pasted.end() == len(prompt) else prompt).strip()
    last = text.rpartition("\n")[2]
    return last[len(NAMING):] if text.startswith(OPENING) and last.startswith(NAMING) else None


def journal_wake(hook, facts):
    """Journals the wake the prompt is, if it is a handoff, and returns the run's verdict."""
    handoff = handoff_named(hook["prompt"])
    if handoff is None:
        # [LAW:nothing-unseen] A prompt that names a handoff's file and is not a handoff quotes
        # one, or is one in a wrapping this does not know, which ends its chain: the log tells it
        # from a prompt that names none.
        return "not a handoff" + (", though it names a handoff's file" if NAMING in hook["prompt"]
                                  else "")
    # The path is recorded as the handoff gives it: a wake finds its close-out by this text.
    # [LAW:single-enforcer] Whether it is a path the journal keeps is `woke`'s to say, and its
    # refusal is worded for a person, so it is the hook's error with no traceback around it.
    try:
        wake = woke(origin(hook["session_id"], hook["cwd"]), handoff)
    except Refused as refusal:
        sys.exit(f"memento journal-wake: {refusal}")
    record(wake, JOURNAL)
    return f"woke from {handoff}"


run("journal-wake", "UserPromptSubmit", journal_wake)
