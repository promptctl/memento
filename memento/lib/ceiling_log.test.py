#!/usr/bin/env python3
"""Tests for a hook's run: what `run` logs for a run that succeeds, one that fails partway, one given
a payload for another event, one whose log is over its cap, and one whose log cannot be written.
Every case runs in process, with its payload on a stdin built here and its log in a scratch
directory.

Run with: python3 ceiling_log.test.py
"""

import atexit
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ceiling-log-test-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ceiling_log  # noqa: E402
from ceiling_log import run  # noqa: E402

failures = []
PAYLOAD = {"session_id": "abcdef0123456789", "hook_event_name": "Stop"}


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def ran(work, payload=PAYLOAD, every_line=lambda hook: {}, log=None):
    """Runs `work` as the hook `test hook` on Stop, logging to `log` or to a log of its own. Returns
    (what it raised or None, the log's lines, stderr)."""
    ceiling_log.LOG_FILE = log or Path(tempfile.mkdtemp(dir=SCRATCH)) / "hook.log"
    sys.stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    stderr = io.StringIO()
    raised = None
    with contextlib.redirect_stderr(stderr):
        try:
            run("test hook", "Stop", work, every_line=every_line)
        except (Exception, SystemExit) as failure:
            raised = failure
    lines = ceiling_log.LOG_FILE.read_text().splitlines() if log is None else []
    return raised, lines, stderr.getvalue()


def decided(hook, facts):
    facts["tokens"] = 12
    facts["ceiling"] = 34
    return "allow-under"


raised, lines, _ = ran(decided, every_line=lambda hook: {"status": "written"})
check("a run that decides logs one line: its session, event, facts in the order work set them, "
      "every line's facts after them, and the verdict",
      raised is None and len(lines) == 1
      and lines[0].endswith(" session=abcdef01 event=Stop tokens=12 ceiling=34 status=written "
                            "-> allow-under"), (raised, lines))

boom = KeyError("transcript_path")


def fails_after_one_fact(hook, facts):
    facts["tokens"] = 12
    raise boom


raised, lines, _ = ran(fails_after_one_fact, every_line=lambda hook: {"status": "written"})
check("a run that fails partway logs stopped with the facts it reached and none it did not, "
      "every line's facts included",
      len(lines) == 1 and lines[0].endswith(
          " tokens=12 status=written -> stopped: KeyError: 'transcript_path'")
      and "ceiling=" not in lines[0], lines)
check("and the failure is re-raised as it was raised", raised is boom, repr(raised))


def refuses(hook, facts):
    sys.exit("memento test hook: refused.")


raised, lines, _ = ran(refuses)
check("an exit the work makes is logged with its message and re-raised",
      isinstance(raised, SystemExit) and raised.code == "memento test hook: refused."
      and len(lines) == 1 and lines[0].endswith("-> stopped: memento test hook: refused."),
      (raised, lines))

calls = []
raised, lines, _ = ran(lambda hook, facts: calls.append(hook),
                       payload={"session_id": "abcdef0123456789", "hook_event_name": "PreToolUse"},
                       every_line=lambda hook: calls.append(("every_line", hook)) or {})
check("a payload for another event exits naming the hook and both events, and neither the work "
      "nor every_line runs", isinstance(raised, SystemExit)
      and "memento test hook: registered on Stop" in raised.code
      and "'PreToolUse'" in raised.code and calls == [], (raised, calls))
check("and its line names the session and event the payload gave, so a grep for the session finds it",
      len(lines) == 1 and " session=abcdef01 event=PreToolUse -> stopped: " in lines[0], lines)

for raw in ("42", "[]", "not json at all"):
    calls = []
    raised, lines, _ = ran(decided, payload=raw,
                           every_line=lambda hook: calls.append(hook) or {"status": "written"})
    check(f"stdin {raw!r}, which is no JSON object, stops the run and is logged with no session "
          "and no fields", raised is not None and calls == [] and len(lines) == 1
          and " session=None event=None -> stopped: " in lines[0], (raised, calls, lines))

full = Path(tempfile.mkdtemp(dir=SCRATCH)) / "hook.log"
seeded = [f"line {n:07d} " + "x" * 80 for n in range(ceiling_log.LOG_CAP // 90 + 1)]
full.write_text("".join(f"{line}\n" for line in seeded))
ran(decided, log=full)
lines = full.read_text().splitlines()
newest = lines[1:-1]
check("a log over the cap, written once more, says older lines were dropped, then holds its newest "
      "lines whole and the new line, and is under the cap",
      lines[0] == f"[older lines dropped at {ceiling_log.LOG_CAP} bytes]"
      and newest and newest == seeded[-len(newest):] and len(newest) < len(seeded)
      and lines[-1].endswith(" -> allow-under")
      and full.stat().st_size < ceiling_log.LOG_CAP, (lines[:2], lines[-2:], full.stat().st_size))

full.write_text(full.read_text() + "".join(f"{line}\n" for line in seeded))
ran(decided, log=full)
lines = full.read_text().splitlines()
check("a log over the cap a second time holds one dropped-lines line, at its top",
      [n for n, line in enumerate(lines) if line.startswith("[older lines dropped")] == [0],
      lines[:2])

under = Path(tempfile.mkdtemp(dir=SCRATCH)) / "hook.log"
under.write_text("an older line\n")
ran(decided, log=under)
lines = under.read_text().splitlines()
check("a log under the cap is appended to and otherwise left as it was",
      len(lines) == 2 and lines[0] == "an older line" and lines[1].endswith(" -> allow-under"), lines)

blocker = SCRATCH / "a file"
blocker.write_text("")
raised, _, stderr = ran(decided, log=blocker / "hook.log")
check("a log that cannot be written is reported on stderr naming the hook, and the run completes",
      raised is None and stderr.startswith("memento test hook: cannot write "), (raised, stderr))

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
