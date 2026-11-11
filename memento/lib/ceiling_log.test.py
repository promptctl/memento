#!/usr/bin/env python3
"""Tests for a hook's run: what `run` logs for a run that succeeds, one that fails partway, one given
a payload for another event, one whose log is over its cap, and one whose log cannot be written.
Every case runs in process, with its payload on a stdin built here and its log in a scratch
directory.

Run with: python3 ceiling_log.test.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402

SCRATCH = Path(hermetic.home("ceiling-log-test-"))
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
    lines = ceiling_log.LOG_FILE.read_text(encoding="utf-8").splitlines() if log is None else []
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


def fails_on_a_multiline_message(hook, facts):
    facts["reply"] = "first\r\nsecond"
    facts["path"] = "C:\\new"
    facts["home"] = "/Users/zoë"
    facts["shot"] = "Shot 10.00\u202fAM \U0001f468\u200d\U0001f469.png"
    raise ValueError("cannot read\nthe transcript")


raised, lines, _ = ran(fails_on_a_multiline_message)
written = ceiling_log.LOG_FILE.read_bytes()
check("a run whose fields and stopped reason carry line breaks logs one line, read either as bytes "
      "split on newline or as text split on every line break, with each break escaped, a backslash "
      "doubled, and non-ASCII that breaks no line, a narrow no-break space and a zero-width joiner "
      "among it, kept as written; the failure is re-raised",
      isinstance(raised, ValueError) and len(lines) == 1 and written.count(b"\n") == 1
      and written.endswith(b"\n")
      and lines[0].endswith(" reply=first\\r\\nsecond path=C:\\\\new home=/Users/zoë "
                            "shot=Shot 10.00\u202fAM \U0001f468\u200d\U0001f469.png "
                            "-> stopped: ValueError: cannot read\\nthe transcript"), (lines, written))

raised, lines, _ = ran(lambda hook, facts: "allow\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029under\x1b[0m")
check("a verdict carrying every character str.splitlines breaks a line at, and a control character, "
      "logs one line, each escaped",
      raised is None and len(lines) == 1 and lines[0].endswith(
          " -> allow\\n\\r\\x0b\\x0c\\x1c\\x1d\\x1e\\x85\\u2028\\u2029under\\x1b[0m"), (raised, lines))

full = Path(tempfile.mkdtemp(dir=SCRATCH)) / "hook.log"
seeded = [f"line {n:07d} " + "x" * 80 for n in range(ceiling_log.LOG_CAP // 90 + 1)]
full.write_text("".join(f"{line}\n" for line in seeded))
ran(decided, log=full)
lines = full.read_text().splitlines()
newest = lines[1:-1]
check("a log over the cap, written once more, says older lines were dropped, then holds its newest "
      "lines whole, as many as fit in half the cap with it, and the new line",
      lines[0] == f"[older lines dropped at {ceiling_log.LOG_CAP} bytes]"
      and newest == seeded[-len(newest):]
      and ceiling_log.LOG_CAP // 2 - len(seeded[0]) - 1 < sum(len(line) + 1 for line in lines[:-1])
      <= ceiling_log.LOG_CAP // 2
      and lines[-1].endswith(" -> allow-under"), (lines[:2], lines[-2:], full.stat().st_size))

full.write_text(full.read_text() + "".join(f"{line}\n" for line in seeded))
ran(decided, log=full)
lines = full.read_text().splitlines()
check("a log over the cap a second time holds one dropped-lines line, at its top",
      [n for n, line in enumerate(lines) if line.startswith("[older lines dropped")] == [0],
      lines[:2])

unended = Path(tempfile.mkdtemp(dir=SCRATCH)) / "hook.log"
unended.write_text("x" * (ceiling_log.LOG_CAP + 1))
ran(decided, log=unended)
lines = unended.read_text().splitlines()
check("a log over the cap with no line ending in its newest half keeps none of it",
      len(lines) == 2 and lines[0] == f"[older lines dropped at {ceiling_log.LOG_CAP} bytes]"
      and not lines[1].startswith("x") and lines[1].endswith(" -> allow-under"),
      [line[:60] for line in lines])

brim = [f"line {n:07d} " + "x" * 80 for n in range(ceiling_log.LOG_CAP // 94 - 1)]
body = "".join(f"{line}\n" for line in brim)
brimful = "p" * (ceiling_log.LOG_CAP - len(body)) + "\n" + body
cut = Path(tempfile.mkdtemp(dir=SCRATCH)) / "hook.log"
cut.write_text(brimful)
ran(decided, log=cut)
written = cut.read_text()
rewritten = written[:written.rstrip("\n").rfind("\n") + 1]
killed = Path(tempfile.mkdtemp(dir=SCRATCH)) / "hook.log"
killed.write_text(rewritten + brimful[len(rewritten):])
ran(decided, log=killed)
lines = killed.read_text().splitlines()
newest = lines[1:-1]
check("a log one byte over the cap whose rewrite was killed before the cut, written once more, "
      "holds its newest lines whole and once, and the new line",
      lines[0] == f"[older lines dropped at {ceiling_log.LOG_CAP} bytes]"
      and newest == brim[-len(newest):] and lines[-1].endswith(" -> allow-under"),
      (len(brimful), lines[:2], [line for line in newest if line not in brim][:2]))

under = Path(tempfile.mkdtemp(dir=SCRATCH)) / "hook.log"
under.write_text("an older line\n")
ran(decided, log=under)
lines = under.read_text().splitlines()
check("a log under the cap is appended to and otherwise left as it was",
      len(lines) == 2 and lines[0] == "an older line" and lines[1].endswith(" -> allow-under"), lines)

raised, lines, _ = ran(decided, payload='{"session_id": "\\ud800abcdefgh", "hook_event_name": "Stop"}')
check("a payload holding a code point that cannot be encoded is logged with it escaped, and the run "
      "completes", raised is None and len(lines) == 1 and " session=\\ud800abcdefg " in lines[0],
      (raised, lines))

blocker = SCRATCH / "a file"
blocker.write_text("")
raised, _, stderr = ran(decided, log=blocker / "hook.log")
check("a log that cannot be written is reported on stderr naming the hook, and the run completes",
      raised is None and stderr.startswith("memento test hook: cannot write "), (raised, stderr))

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
