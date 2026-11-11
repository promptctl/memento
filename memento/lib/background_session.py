#!/usr/bin/env python3
"""Reads which background session a Claude Code session is, from `claude agents --json`, the listing of
every session on the machine, and stops one. A background session is one `claude --bg` started and a
daemon hosts; its entry has `kind` "background", its `sessionId`, and the `id` that `claude stop` and
`claude attach` take (2.1.299). finalize-session retires a daemon-hosted session with `claude stop <id>`,
since the daemon respawns one whose process exits any other way. Both ask the daemon, so each is given
ANSWER_SECONDS to answer: a launcher waits on one inside an agent's Bash call, and a worker's later steps
wait behind the other. Run as a script:

  background_session.py id <claude> <session id>
      prints the session's background session id and exits 0, or prints why it cannot on stderr and
      exits 2.
  background_session.py stop <claude> <background session id>
      prints what `claude stop` said and exits 0 once it has stopped the session, or prints why it has
      not on stderr and exits 2.
"""

import json
import os
import subprocess
import sys

ANSWER_SECONDS = 10


class DaemonError(Exception):
    """The daemon did not give what was asked of it; the message says why."""


def background_id(listing, session_id):
    """The id of the one background session the listing holds for `session_id`. [LAW:parse-dont-validate]
    Anything else, a listing that is not a JSON array of objects included, raises DaemonError."""
    try:
        entries = json.loads(listing)
    except ValueError as error:
        raise DaemonError(f"the session listing is not JSON: {error}") from None
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise DaemonError("the session listing is not a JSON array of sessions")
    ids = [entry.get("id") for entry in entries
           if entry.get("kind") == "background" and entry.get("sessionId") == session_id]
    if len(ids) != 1:
        raise DaemonError(f"the session listing holds {len(ids)} background sessions for session {session_id}")
    if not (isinstance(ids[0], str) and ids[0]):
        raise DaemonError(f"the background session for session {session_id} is listed with no id")
    return ids[0]


def ask(claude, *args, timeout=ANSWER_SECONDS):
    """What `claude <args>` prints once it has exited 0 within `timeout` seconds. Running past it, or exiting
    any other way, raises DaemonError with what it said."""
    asked = " ".join((claude, *args))
    try:
        done = subprocess.run([claude, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DaemonError(f"'{asked}' did not answer within {timeout}s") from None
    if done.returncode != 0:
        said = "; ".join(stream.strip() for stream in (done.stderr, done.stdout) if stream.strip())
        raise DaemonError(f"'{asked}' exited {done.returncode}: {said}")
    return done.stdout


def _id(claude, session_id):
    print(background_id(ask(claude, "agents", "--json"), session_id))
    return 0


def _stop(claude, background_session_id):
    print(ask(claude, "stop", background_session_id).strip())
    return 0


COMMANDS = {"id": _id, "stop": _stop}


if __name__ == "__main__":
    # -I keeps this file's directory off the path; lib/script.py is beside it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import script
    script.run(COMMANDS, DaemonError)
