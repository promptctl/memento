#!/usr/bin/env python3
"""Reads which background session a Claude Code session is, from `claude agents --json`, the listing of
every session on the machine. A background session is one `claude --bg` started and a daemon hosts; its
entry has `kind` "background", its `sessionId`, and the `id` that `claude stop` and `claude attach` take
(2.1.299). finalize-session retires a daemon-hosted session with `claude stop <id>`, since the daemon
respawns one whose process exits any other way. Run as a script:

  background_session.py id <session id>   (the listing on stdin)
      prints the session's background session id and exits 0, or prints why it cannot on stderr and
      exits 2.
"""

import json
import os
import sys


class Unlisted(Exception):
    """The listing does not name one background session for the session; the message says why."""


def background_id(listing, session_id):
    """The id of the one background session the listing holds for `session_id`. [LAW:parse-dont-validate]
    Anything else, a listing that is not a JSON array of objects included, raises Unlisted."""
    try:
        entries = json.loads(listing)
    except ValueError as error:
        raise Unlisted(f"the session listing is not JSON: {error}") from None
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise Unlisted("the session listing is not a JSON array of sessions")
    ids = [entry.get("id") for entry in entries
           if entry.get("kind") == "background" and entry.get("sessionId") == session_id]
    if len(ids) != 1:
        raise Unlisted(f"the session listing holds {len(ids)} background sessions for session {session_id}")
    if not (isinstance(ids[0], str) and ids[0]):
        raise Unlisted(f"the background session for session {session_id} is listed with no id")
    return ids[0]


def _id(session_id):
    print(background_id(sys.stdin.read(), session_id))
    return 0


COMMANDS = {"id": _id}


if __name__ == "__main__":
    # -I keeps this file's directory off the path; lib/script.py is beside it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import script
    script.run(COMMANDS, Unlisted)
