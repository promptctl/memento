#!/usr/bin/env python3
"""Tests for the background-session reader: which entry of `claude agents --json` names a session's
background session, and every way the listing can fail to say. The reader runs in process, and the
script form runs once per answer as the launcher runs it.

Run with: python3 background_session.test.py
"""

import json
import os
import subprocess
import sys

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
from background_session import Unlisted, background_id  # noqa: E402

SESSION = "b08daab6-66a8-47af-9d13-aaa85780e908"
failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def background(id, session=SESSION):
    """A background session's entry, as 2.1.299 lists one: no pid."""
    return {"id": id, "cwd": "/tmp", "kind": "background", "startedAt": 1794394494195,
            "sessionId": session, "name": "a job", "state": "working"}


def interactive(session=SESSION):
    return {"pid": 4242, "cwd": "/tmp", "kind": "interactive", "startedAt": 1794394494195,
            "sessionId": session, "name": "a session", "status": "idle"}


def refusal(listing):
    try:
        return f"answered {background_id(listing, SESSION)!r}"
    except Unlisted as error:
        return str(error)


listing = json.dumps([interactive("another"), background("b08daab6"), background("d239d02d", "another")])
check("a session the listing holds as a background session is named by that entry's id",
      background_id(listing, SESSION) == "b08daab6", background_id(listing, SESSION))

for name, bad, reason in (
        ("an interactive session of the same id", json.dumps([interactive()]), "0 background sessions"),
        ("a listing that holds only other sessions' background sessions",
         json.dumps([background("d239d02d", "another")]), "0 background sessions"),
        ("an empty listing", "[]", "0 background sessions"),
        ("a listing that is not JSON", "Error: daemon not running", "not JSON"),
        ("a listing that is not an array", json.dumps({"id": "b08daab6"}), "not a JSON array"),
        ("a listing holding something other than sessions", json.dumps([background("b08daab6"), "x"]),
         "not a JSON array"),
        ("two background sessions for one session", json.dumps([background("b08daab6"), background("c1")]),
         "2 background sessions"),
        ("a background session listed with no id", json.dumps([background(None)]), "with no id"),
        ("a background session listed with an empty id", json.dumps([background("")]), "with no id")):
    found = refusal(bad)
    check(f"{name} names no background session, saying why", reason in found, found)


def script(*args, stdin=""):
    return subprocess.run([sys.executable, "-I", os.path.join(LIB, "background_session.py"), *args],
                          input=stdin, text=True, capture_output=True)


done = script("id", SESSION, stdin=listing)
check("id prints the background session's id and exits 0",
      done.returncode == 0 and done.stdout == "b08daab6\n" and done.stderr == "", done)
for stdin, reason in (("[]", "0 background sessions"), ("Error: daemon not running", "not JSON")):
    done = script("id", SESSION, stdin=stdin)
    check(f"and exits 2 with the reason when the listing names none: {stdin}",
          done.returncode == 2 and done.stdout == "" and reason in done.stderr, done)
for args in ((), ("id",), ("bogus", SESSION)):
    done = script(*args)
    check(f"the script refuses a malformed call with exit 2: {' '.join(args) or '(none)'}",
          done.returncode == 2 and "usage:" in done.stderr, done)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
