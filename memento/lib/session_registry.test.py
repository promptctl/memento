#!/usr/bin/env python3
"""Tests for the session registry reader: which session a claude process is in, and every way its entry
can fail to say. The reader runs in process against entries written under a scratch config directory,
and the script form runs as the launcher runs it.

Run with: python3 session_registry.test.py
"""

import json
import os
import subprocess
import sys
import tempfile

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import hermetic  # noqa: E402

hermetic.home()
from session_registry import Unregistered, current  # noqa: E402

PID = "74101"
SESSION = "6499992e-89be-4ade-9818-73a39e9af46c"
failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def registry(text=None):
    """A config directory whose registry holds an entry for PID with this text, or no entry for None."""
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "sessions"))
    if text is not None:
        with open(os.path.join(root, "sessions", PID + ".json"), "w") as handle:
            handle.write(text)
    return root


def refusal(root):
    try:
        return f"no refusal: {current(root, PID)}"
    except Unregistered as error:
        return str(error)


# The fields beside sessionId are what 2.1.299 writes; the reader takes only the session.
ENTRY = {"pid": int(PID), "sessionId": SESSION, "cwd": "/work", "kind": "interactive",
         "status": "busy", "updatedAt": 1794434070856}

root = registry(json.dumps(ENTRY))
check("the session the process's entry names is read", current(root, PID) == SESSION, refusal(root))

root = registry()
check("no entry for the process is refused, naming the entry",
      refusal(root).startswith("no registry entry " + os.path.join(root, "sessions", PID + ".json")),
      refusal(root))
for torn in ("", '{"pid": 74101, "sessionId": "6499'):
    root = registry(torn)
    check(f"an entry that is not JSON is refused, as a read during a rewrite sees it: {torn!r}",
          "is not a JSON object" in refusal(root), refusal(root))
root = registry("[]")
check("an entry that is JSON but no object is refused", "is not a JSON object" in refusal(root), refusal(root))
for bad in (None, "", 7):
    fields = {k: v for k, v in ENTRY.items() if k != "sessionId"}
    root = registry(json.dumps(fields if bad is None else {**fields, "sessionId": bad}))
    check(f"an entry that names no session is refused: {bad!r}", "names no session" in refusal(root),
          refusal(root))

# --- the script form, as the worker runs it ------------------------------------

SCRIPT = os.path.join(LIB, "session_registry.py")


def script(*args):
    return subprocess.run([sys.executable, "-I", SCRIPT, *args], capture_output=True, text=True)


root = registry(json.dumps(ENTRY))
done = script("current", root, PID)
check("current prints the session id and exits 0",
      done.returncode == 0 and done.stdout == SESSION + "\n" and done.stderr == "", done)
done = script("current", registry(), PID)
check("and exits 2 with the reason, printing nothing on stdout",
      done.returncode == 2 and done.stdout == "" and "no registry entry" in done.stderr, done)
done = script("current", root)
check("a malformed call is refused with exit 2", done.returncode == 2 and "usage:" in done.stderr, done)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
