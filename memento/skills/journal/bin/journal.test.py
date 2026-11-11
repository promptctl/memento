#!/usr/bin/env python3
"""Tests for the `journal` command: that each verb reaches the journal with the calling session's
origin, and what each exit code means. The chain and the record are proven in `lib/journal.test.py`;
this runs the real command as a subprocess against a scratch journal. Run with: python3 journal.test.py
"""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="journal-cli-test-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "journal")
PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(PLUGIN, "lib"))
from journal import SUMMARY_CAP, Close, Note, Origin, entries, record, woke  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def scratch_journal():
    return Path(tempfile.mkdtemp(dir=SCRATCH)) / "journal.jsonl"


def run(journal, *argv, session="session-A", cwd=SCRATCH):
    """Runs the command as a session's Bash tool does. `session=None` is an environment naming none."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["MEMENTO_JOURNAL"] = str(journal)
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    return subprocess.run([sys.executable, CLI, *argv], text=True, capture_output=True, env=env,
                          cwd=cwd)


repo = SCRATCH / "repo"
subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/journal-lane"], cwd=repo, check=True,
               capture_output=True)

# --- note and close write the calling session's entry -------------------------------------------

journal = scratch_journal()
done = run(journal, "note", "the parser\nloses a byte", cwd=repo)
kept = entries(journal)
check("note records the text for the calling session, where it is working",
      done.returncode == 0 and done.stdout == "" and len(kept) == 1 and isinstance(kept[0], Note)
      and (kept[0].session, kept[0].branch, kept[0].text)
      == ("session-A", "journal-lane", "the parser\nloses a byte")
      and os.path.samefile(kept[0].cwd, repo), (done, kept))

done = run(journal, "note", "several", "words", "unquoted")
check("a note given as several words is one note",
      done.returncode == 0 and entries(journal)[-1].text == "several words unquoted", done)

done = run(journal, "close", "--handoff", "/handoffs/1.md", "--summary=- shipped the parser")
last = entries(journal)[-1]
check("close records the handoff written and the summary, which may open with a dash",
      done.returncode == 0 and isinstance(last, Close)
      and (last.session, last.handoff, last.summary)
      == ("session-A", "/handoffs/1.md", "- shipped the parser"), (done, last))

done = run(journal, "note", "from nowhere", session=None)
check("a note from an environment that names no session is kept, with the session absent",
      done.returncode == 0 and entries(journal)[-1].session is None, done)

# --- text the journal does not take --------------------------------------------------------------

journal = scratch_journal()
for name, argv in (("an empty note", ("note", "  ")),
                   ("an empty summary", ("close", "--handoff", "/h.md", "--summary", "")),
                   ("a summary over the cap",
                    ("close", "--handoff", "/h.md", "--summary", "x" * (SUMMARY_CAP + 1))),
                   ("a close-out with no summary", ("close", "--handoff", "/h.md")),
                   ("a filter on a field no entry has", ("show", "mood=good")),
                   ("no verb", ())):
    done = run(journal, *argv)
    check(f"{name} exits 2, says why, and writes nothing",
          done.returncode == 2 and "journal" in done.stderr and not journal.exists(), done)

# --- recall --------------------------------------------------------------------------------------

journal = scratch_journal()
run(journal, "note", "a dead end worth keeping", session="session-A")
run(journal, "close", "--handoff", "/handoffs/1.md", "--summary", "shipped the parser",
    session="session-A", cwd=repo)
record(woke(Origin("2026-11-10T00:00:00Z", "session-B", "/work", None), "/handoffs/1.md"), journal)
done = run(journal, "recall", session="session-B")
check("recall prints the summary of the session whose handoff woke this one, and not its note",
      done.returncode == 0 and "  shipped the parser\n" in done.stdout
      and "(journal-lane)" in done.stdout and "dead end" not in done.stdout
      and done.stdout.endswith("Nothing earlier: session session-A did not wake from a handoff.\n"),
      done)
done = run(journal, "recall", session="session-A")
check("recall in a session that did not wake from a handoff says so and exits 0",
      done.returncode == 0
      and done.stdout == "Nothing earlier: session session-A did not wake from a handoff.\n", done)
done = run(journal, "recall", session=None)
check("recall with no session named exits 1 and says what is missing",
      done.returncode == 1 and "CLAUDE_CODE_SESSION_ID is not set" in done.stderr
      and done.stdout == "", done)

# --- show ----------------------------------------------------------------------------------------

run(journal, "note", "another agent's note", session="session-elsewhere")
done = run(journal, "show")
check("show prints every entry, oldest first",
      done.returncode == 0 and [line.split("  ")[1] for line in done.stdout.splitlines()
                                if not line.startswith(" ")] == ["note", "close", "wake", "note"],
      done)
done = run(journal, "show", "--chain", "session-B")
check("show --chain keeps the entries of that session's chain and no other session's",
      done.returncode == 0 and "a dead end worth keeping" in done.stdout
      and "shipped the parser" in done.stdout and "another agent" not in done.stdout, done)
done = run(journal, "show", "kind=note", "text=dead end")
check("each FIELD=TEXT narrows the entries shown",
      done.returncode == 0 and "a dead end worth keeping" in done.stdout
      and "another agent" not in done.stdout and "shipped" not in done.stdout, done)
done = run(scratch_journal(), "show")
check("show of a journal nobody has written to prints nothing and exits 0",
      done.returncode == 0 and done.stdout == "", done)

# --- a journal that cannot be read or written ----------------------------------------------------

with open(journal, "a") as handle:
    handle.write("not an entry\n")
for verb in ("recall", "show"):
    done = run(journal, verb, session="session-B")
    check(f"{verb} of a journal with a line that is not an entry exits 1 naming the line",
          done.returncode == 1 and f"{journal} line 5 is not a journal entry" in done.stderr
          and done.stdout == "", done)
blocked = SCRATCH / "a-file"
blocked.write_text("")
done = run(blocked / "journal.jsonl", "note", "nowhere to go")
check("a journal that cannot be written exits 1 with the reason",
      done.returncode == 1 and done.stderr.startswith("journal: "), done)

check("the skill that documents it names it",
      "bin/journal" in open(os.path.join(os.path.dirname(HERE), "SKILL.md")).read())

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
