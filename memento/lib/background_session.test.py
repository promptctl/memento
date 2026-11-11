#!/usr/bin/env python3
"""Tests for the background-session reader: which entry of `claude agents --json` names a session's
background session, every way the listing can fail to say, and how an ask of the daemon ends. The reader
runs in process, and the script form runs once per answer as the launcher and its worker run it, over a
fake claude.

Run with: python3 background_session.test.py
"""

import json
import os
import subprocess
import sys
import tempfile

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
from background_session import DaemonError, ask, background_id  # noqa: E402

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
    except DaemonError as error:
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


# Stands in for claude: `agents --json` prints $FAKE_AGENTS, `stop <id>` says it stopped it, and either
# exits $FAKE_STATUS after sleeping $FAKE_SLEEP seconds.
CLAUDE = os.path.join(tempfile.mkdtemp(prefix="background-session."), "claude")
with open(CLAUDE, "w") as handle:
    handle.write("""#!/bin/sh
sleep "${FAKE_SLEEP:-0}"
case "$1" in
  agents) printf '%s\\n' "$FAKE_AGENTS" ;;
  stop) echo "Stopped $2" ;;
esac
echo "a complaint" >&2
exit "${FAKE_STATUS:-0}"
""")
os.chmod(CLAUDE, 0o755)


def asked(*args, timeout=5, **env):
    os.environ.update({"FAKE_SLEEP": "0", "FAKE_STATUS": "0", **env})
    try:
        return ask(CLAUDE, *args, timeout=timeout)
    except DaemonError as error:
        return str(error)


check("an ask the daemon answers gives what it printed", asked("stop", "b08daab6") == "Stopped b08daab6\n",
      asked("stop", "b08daab6"))
found = asked("stop", "ffffffff", FAKE_STATUS="1")
check("an ask the daemon refuses raises, with its exit status and what it said",
      f"'{CLAUDE} stop ffffffff' exited 1: a complaint" in found and "Stopped ffffffff" in found, found)
found = asked("agents", "--json", timeout=0.5, FAKE_SLEEP="5")
check("an ask the daemon does not answer in time raises, naming the wait",
      found == f"'{CLAUDE} agents --json' did not answer within 0.5s", found)


def script(*args, **env):
    return subprocess.run([sys.executable, "-I", os.path.join(LIB, "background_session.py"), *args],
                          text=True, capture_output=True,
                          env={**os.environ, "FAKE_SLEEP": "0", "FAKE_STATUS": "0", **env})


done = script("id", CLAUDE, SESSION, FAKE_AGENTS=listing)
check("id prints the background session's id and exits 0",
      done.returncode == 0 and done.stdout == "b08daab6\n" and done.stderr == "", done)
for env, reason in ((dict(FAKE_AGENTS="[]"), "0 background sessions"),
                    (dict(FAKE_AGENTS="Error: daemon not running"), "not JSON"),
                    (dict(FAKE_AGENTS=listing, FAKE_STATUS="1"), "exited 1")):
    done = script("id", CLAUDE, SESSION, **env)
    check(f"and exits 2 with the reason when the listing names none: {env}",
          done.returncode == 2 and done.stdout == "" and reason in done.stderr, done)
done = script("stop", CLAUDE, "b08daab6")
check("stop prints what the daemon said and exits 0 once it has stopped the session",
      done.returncode == 0 and done.stdout == "Stopped b08daab6\n" and done.stderr == "", done)
done = script("stop", CLAUDE, "ffffffff", FAKE_STATUS="1")
check("and exits 2 with what the daemon said when it does not",
      done.returncode == 2 and done.stdout == "" and "exited 1: a complaint" in done.stderr, done)
for args in ((), ("id", CLAUDE), ("stop", CLAUDE), ("bogus", SESSION)):
    done = script(*args)
    check(f"the script refuses a malformed call with exit 2: {' '.join(args) or '(none)'}",
          done.returncode == 2 and "usage:" in done.stderr, done)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
