#!/usr/bin/env python3
"""Reads Claude Code's session registry, `<config dir>/sessions/<pid>.json`: one entry per running claude
process, naming the session that process is in now. A /clear moves the process to a new session and
rewrites its entry, so finalize-session's tmux worker sees its reset land here: the entry belongs to the
pane's own claude, where a banner on its screen may be the old session's and another pane's /clear is
another process's. Run as a script:

  session_registry.py current <config dir> <pid>
      prints the id of the session claude process <pid> is in and exits 0.

It prints why it cannot answer on stderr and exits 2.
"""

import json
import os
import sys


class Unregistered(Exception):
    """The registry holds no readable session for the process; the message names the entry and why."""


def entry(config_dir, pid):
    return os.path.join(config_dir, "sessions", f"{pid}.json")


def current(config_dir, pid):
    """The id of the session claude process `pid` is in, as its registry entry names it.

    [LAW:parse-dont-validate] An id comes back as a nonempty string, or this raises Unregistered: no entry,
    an entry that is not JSON, which a read that meets a rewrite in progress can see, or one that names
    no session."""
    path = entry(config_dir, pid)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise Unregistered(f"no registry entry {path}, so claude process {pid} is not running "
                           "under this config dir") from None
    try:
        session = json.loads(text).get("sessionId")
    except (ValueError, AttributeError):
        raise Unregistered(f"registry entry {path} is not a JSON object: {text[:200]!r}") from None
    if not isinstance(session, str) or not session:
        raise Unregistered(f"registry entry {path} names no session: sessionId is {session!r}")
    return session


def _current(config_dir, pid):
    print(current(config_dir, pid))
    return 0


COMMANDS = {"current": _current}


if __name__ == "__main__":
    # -I keeps this file's directory off the path; lib/script.py is beside it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import script
    script.run(COMMANDS, Unregistered)
