#!/usr/bin/env python3
"""The record that a session has handed off: its successor took the message, so the work is the
successor's, and this session is retired or being retired. finalize-session's detached worker marks a
session once its successor has taken the message and clears the mark once the old process is retired;
the handed-off hook denies every tool call a marked session makes, so the old context acts on nothing
while its retirement lands, or after one that failed. [LAW:one-source-of-truth] The record's place is
defined here alone, for the worker that writes it and the hook that reads it. Run as a script:

  handed_off.py mark <session id> <successor>
      records that the session handed off to the successor the text names, prints the record's path,
      and exits 0.
  handed_off.py clear <session id>
      removes the session's record, and exits 0 whether or not it had one.
"""

import os
import sys


def path(session_id):
    """Where the record for `session_id` lives. HOME is read at each call, so a suite's scratch HOME holds
    its records."""
    return os.path.join(os.path.expanduser("~"), ".claude", "memento", "handed-off", session_id)


def reason(session_id):
    """The successor the session handed off to, as the worker recorded it, or None when the session has
    no record, which is also so when a file stands where the records' directory goes. A record that stands
    but cannot be read raises OSError."""
    try:
        with open(path(session_id), encoding="utf-8") as record:
            return record.read()
    except (FileNotFoundError, NotADirectoryError):
        return None


def mark(session_id, successor):
    # A record stands whole or not at all: it is written beside its place and renamed onto it, so a write
    # that fails leaves only the staged file, which nothing reads, and the worker that hears the failure
    # is right that the session is not fenced.
    os.makedirs(os.path.dirname(path(session_id)), exist_ok=True)
    staged = path(session_id) + ".staged"
    with open(staged, "w", encoding="utf-8") as record:
        record.write(successor)
    os.replace(staged, path(session_id))
    print(path(session_id))
    return 0


def clear(session_id):
    try:
        os.remove(path(session_id))
    except FileNotFoundError:
        pass
    return 0


COMMANDS = {"mark": mark, "clear": clear}


if __name__ == "__main__":
    # -I keeps this file's directory off the path; lib/script.py is beside it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import script
    script.run(COMMANDS, OSError)
