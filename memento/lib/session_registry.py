#!/usr/bin/env python3
"""Reads Claude Code's session registry, `<config dir>/sessions/<pid>.json`: one entry per running claude
process, naming the session that process is in now. A /clear moves the process to a new session and
rewrites its entry, so finalize-session's tmux worker sees its reset land here: the entry belongs to the
pane's own claude, where a banner on its screen may be the old session's and another pane's /clear is
another process's. Run as a script:

  session_registry.py changed <config dir> <pid> <before> <timeout seconds> <poll seconds>
      waits for claude process <pid> to leave session <before>, prints the move and exits 0.
      A process with no entry has exited, so the wait ends there.

It prints why it cannot answer on stderr and exits 2.
"""

import json
import os
import sys
import time


class Unseen(Exception):
    """The registry cannot show what was asked of it; the message says why."""


class Unregistered(Unseen):
    """The registry holds no readable session for the process; the message names the entry and why."""


class Torn(Unregistered):
    """The entry is not a JSON object, as a read that meets a rewrite in progress sees it: Claude Code writes
    the entry in place, and its own reader rereads a record it finds torn (2.1.299)."""


class Unchanged(Unseen):
    """The process was still in the session it started in, or unreadable, when the wait ran out."""


def entry(config_dir, pid):
    return os.path.join(config_dir, "sessions", f"{pid}.json")


def current(config_dir, pid):
    """The id of the session claude process `pid` is in, as its registry entry names it.

    [LAW:parse-dont-validate] An id comes back as a nonempty string, or this raises Unregistered: no entry,
    an entry that is not JSON (Torn), or one that names no session."""
    path = entry(config_dir, pid)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        raise Unregistered(f"no registry entry {path}, so claude process {pid} is not running "
                           "under this config dir") from None
    try:
        # A read torn inside a multibyte character fails to decode, which is a ValueError too.
        session = json.loads(data).get("sessionId")
    except (ValueError, AttributeError):
        raise Torn(f"registry entry {path} is not a JSON object: {data[:200]!r}") from None
    if not isinstance(session, str) or not session:
        raise Unregistered(f"registry entry {path} names no session: sessionId is {session!r}")
    return session


def changed(config_dir, pid, before, timeout, interval, sleep=time.sleep):
    """The session claude process `pid` moved to from `before`, and the seconds the wait took, reading
    its entry every `interval` seconds for up to `timeout`.

    A torn read is not yet, and the last one's reason is kept for the Unchanged the wait ends in. Any
    other Unregistered ends the wait: a process with no entry has exited, and its pid may be reissued."""
    start = time.monotonic()
    unread = ""
    while True:
        try:
            now = current(config_dir, pid)
        except Torn as error:
            unread = f"; the last read failed: {error}"
        else:
            unread = ""
            if now != before:
                return now, time.monotonic() - start
        if time.monotonic() - start >= timeout:
            raise Unchanged(f"claude process {pid} did not leave session {before} within {timeout:g}s{unread}")
        sleep(interval)


def _changed(config_dir, pid, before, timeout_seconds, poll_seconds):
    if not before:
        raise Unchanged(f"no session was named for claude process {pid} to leave, so its move cannot be seen")
    now, waited = changed(config_dir, pid, before, float(timeout_seconds), float(poll_seconds))
    print(f"claude process {pid} moved from session {before} to {now} after {waited:.1f}s")
    return 0


COMMANDS = {"changed": _changed}


if __name__ == "__main__":
    # -I keeps this file's directory off the path; lib/script.py is beside it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import script
    script.run(COMMANDS, Unseen)
