#!/usr/bin/env python3
"""Tests for the `journal` command: that each verb reaches the journal with the calling session's
origin, and what each exit code means. The chain and the record are proven in `lib/journal.test.py`;
this runs the real command as a subprocess against a scratch journal. Run with: python3 journal.test.py
"""

import atexit
import os
import shutil
import signal
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
from journal import (NOTE_CAP, RECALLED, SUMMARY_CAP, Close, Note, Origin, Summary,  # noqa: E402
                     entries, noted, record, woke)

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


def summarize(journal, text, **how):
    """Records a summary as an agent does: written to a file, and given to the command by path."""
    path = Path(tempfile.mkdtemp(dir=SCRATCH)) / "summary.md"
    path.write_text(text)
    return run(journal, "summary", "--file", str(path), **how)


repo = SCRATCH / "repo"
subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/journal-lane"], cwd=repo, check=True,
               capture_output=True)

# --- note, summary and close write the calling session's entry ----------------------------------

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

# A note's words share the command line with its options, and a note may open with a dash.
for text in ("-h flag breaks the build", "--fi=nal answer", "--help me decide"):
    done = run(journal, "note", text)
    check(f"a note that opens with {text.split()[0]} is recorded as text, not read as an option",
          done.returncode == 0 and done.stdout == "" and entries(journal)[-1].text == text, done)

# Text that Claude Code's worktree guard refuses in a command reaches the journal in a file.
guarded = "Ran git pull --rebase, then bash -n on the launcher.\nIt didn't help: `sh` isn't $SHELL."
(SCRATCH / "note.md").write_text(guarded)
done = run(journal, "note", "--file", str(SCRATCH / "note.md"))
check("a note given as a file is recorded as it was written",
      done.returncode == 0 and entries(journal)[-1].text == guarded, (done, entries(journal)[-1]))
done = run(journal, "show", "text=git pull")
check("and is found by the words in it",
      done.returncode == 0 and "  text: Ran git pull --rebase, then bash -n" in done.stdout
      and "didn't help" in done.stdout and "several words" not in done.stdout, done)

done = run(journal, "note", "from nowhere", session=None)
check("a note from an environment that names no session is kept, with the session absent",
      done.returncode == 0 and entries(journal)[-1].session is None, done)

journal = scratch_journal()
did = "- Ran git pull --rebase, then bash -n on the launcher.\n\nIt holds: `sh` isn't $SHELL."
done = summarize(journal, f"\n{did}\n\n\n", cwd=repo)
kept = entries(journal)
check("summary records the file's text for the calling session, without its surrounding whitespace",
      done.returncode == 0 and done.stdout == "" and len(kept) == 1 and isinstance(kept[0], Summary)
      and (kept[0].session, kept[0].branch, kept[0].summary) == ("session-A", "journal-lane", did),
      (done, kept))
done = run(journal, "close", "--handoff", "/handoffs/1.md")
kept = entries(journal)
check("close records the handoff written, with the summary the session recorded",
      done.returncode == 0 and done.stdout == "" and len(kept) == 2 and isinstance(kept[1], Close)
      and (kept[1].session, kept[1].handoff, kept[1].summary)
      == ("session-A", "/handoffs/1.md", did), (done, kept))
done = run(journal, "close", "--handoff", "/handoffs/2.md")
check("a second close-out exits 2, says how to record a summary, and writes nothing",
      done.returncode == 2 and "since its last close-out" in done.stderr
      and "journal summary --file <path>" in done.stderr and entries(journal) == kept, done)
summarize(journal, "a first draft")
summarize(journal, "the retry")
# A close-out the journal refuses for its handoff leaves the summary for the one that follows.
done = run(journal, "close", "--handoff", "")
check("a close-out with no handoff path exits 2, says why, and writes nothing",
      done.returncode == 2 and "not absolute" in done.stderr and len(entries(journal)) == 4, done)
done = run(journal, "close", "--handoff", "/handoffs/2.md")
last = entries(journal)[-1]
check("of two summaries a close-out holds the later",
      done.returncode == 0 and (last.handoff, last.summary) == ("/handoffs/2.md", "the retry"),
      (done, last))

# An environment that names no session closes out with the summary recorded the same way.
journal = scratch_journal()
summarize(journal, "did it unnamed", session=None)
summarize(journal, "did A")
done = run(journal, "close", "--handoff", "/handoffs/1.md", session=None)
last = entries(journal)[-1]
check("a close-out that names no session holds the summary recorded with none",
      done.returncode == 0 and (last.session, last.summary) == (None, "did it unnamed"),
      (done, last))

# --- text the journal does not take --------------------------------------------------------------

journal = scratch_journal()
(SCRATCH / "empty.md").write_text("\n")
(SCRATCH / "long.md").write_text("x" * (SUMMARY_CAP + 1))
for name, argv in (("an empty note", ("note", "  ")),
                   ("no note at all", ("note",)),
                   ("a note whose file is empty", ("note", "--file", str(SCRATCH / "empty.md"))),
                   ("a note given both as words and as a file",
                    ("note", "words", "--file", str(SCRATCH / "note.md"))),
                   ("a note given as words and as a file with no name",
                    ("note", "words", "--file", "")),
                   ("a one-word note that reads as an option", ("note", "-x")),
                   ("a summary whose file is empty", ("summary", "--file", str(SCRATCH / "empty.md"))),
                   ("a summary over the cap", ("summary", "--file", str(SCRATCH / "long.md"))),
                   ("a summary given as words", ("summary", "shipped", "the", "parser")),
                   ("a summary given no file", ("summary",)),
                   ("a close-out with no summary recorded", ("close", "--handoff", "/h.md")),
                   ("a filter on a field no entry has", ("show", "mood=good")),
                   ("no verb", ())):
    done = run(journal, *argv)
    check(f"{name} exits 2, says why, and writes nothing",
          done.returncode == 2 and "journal" in done.stderr and not journal.exists(), done)

# --- recall --------------------------------------------------------------------------------------

journal = scratch_journal()
run(journal, "note", "a dead end worth keeping", session="session-A")
summarize(journal, "shipped the parser", session="session-A")
run(journal, "close", "--handoff", "/handoffs/1.md", session="session-A", cwd=repo)
record(woke(Origin("2026-11-10T00:00:00Z", "session-B", "/work", None), "/handoffs/1.md"), journal)
done = run(journal, "recall", session="session-B")
check("recall prints the summary of the session whose handoff woke this one, and not its note",
      done.returncode == 0 and "  shipped the parser\n" in done.stdout
      and "(journal-lane)" in done.stdout and "dead end" not in done.stdout
      and done.stdout.endswith("Nothing earlier: the journal holds no wake for session session-A "
                               "before the earliest close-out shown.\n"), done)
done = run(journal, "recall", session="session-A")
check("recall in a session the journal holds no wake for says so and exits 0",
      done.returncode == 0
      and done.stdout == "Nothing earlier: the journal holds no wake for session session-A.\n", done)
done = run(journal, "recall", session=None)
check("recall with no session named exits 1 and says what is missing",
      done.returncode == 1 and "CLAUDE_CODE_SESSION_ID is not set" in done.stderr
      and done.stdout == "", done)

# --- show ----------------------------------------------------------------------------------------

run(journal, "note", "another agent's note", session="session-elsewhere")
done = run(journal, "show")
check("show prints every entry, oldest first, a close-out's text under its summary and its close",
      done.returncode == 0 and [line.split("  ")[1] for line in done.stdout.splitlines()
                                if not line.startswith(" ")]
      == ["note", "summary", "close", "wake", "note"]
      and done.stdout.count("  summary: shipped the parser\n") == 2, done)
done = run(journal, "show", "--chain", "session-B")
check("show --chain keeps the entries of that session's chain and no other session's",
      done.returncode == 0 and "a dead end worth keeping" in done.stdout
      and "shipped the parser" in done.stdout and "another agent" not in done.stdout
      and done.stdout.endswith("The chain goes no further: the journal holds no wake for session "
                               "session-A before the earliest close-out shown.\n"), done)
done = run(journal, "show", "--chain", "session-nobody")
check("show --chain of a session the journal has never seen says so instead of printing nothing",
      done.returncode == 0 and done.stdout == "The chain goes no further: the journal holds no "
                                              "wake for session session-nobody.\n", done)
done = run(journal, "show", "--chain", "")
check("show --chain of an empty session id shows no entry, where it showed the whole journal",
      done.returncode == 0 and done.stdout.startswith("The chain goes no further: ")
      and len(done.stdout.splitlines()) == 1, done)
done = run(journal, "show", "kind=note", "text=dead end")
check("each FIELD=TEXT narrows the entries shown",
      done.returncode == 0 and "a dead end worth keeping" in done.stdout
      and "another agent" not in done.stdout and "shipped" not in done.stdout, done)
done = run(scratch_journal(), "show")
check("show of a journal nobody has written to prints nothing and exits 0",
      done.returncode == 0 and done.stdout == "", done)

# A reader that stops early: `show` prints the whole record, so `show | head` is its ordinary use.
long = scratch_journal()
for n in range(200):
    record(noted(Origin("2026-11-10T00:00:00Z", "session-A", "/work", None), f"{n} " + "x" * 1000),
           long)
env = dict(os.environ, MEMENTO_JOURNAL=str(long))
showing = subprocess.Popen([sys.executable, CLI, "show"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env)
first = showing.stdout.readline()
showing.stdout.close()
complaint = showing.stderr.read()
showing.stderr.close()
check("show ends without complaint when its reader stops reading, as cat does",
      showing.wait() == -signal.SIGPIPE and complaint == b"" and first.endswith(b"/work\n"),
      (showing.returncode, complaint, first))

# --- a journal that cannot be read or written ----------------------------------------------------

with open(journal, "a") as handle:
    handle.write("not an entry\n")
for verb in ("recall", "show"):
    done = run(journal, verb, session="session-B")
    check(f"{verb} of a journal with a line that is not an entry exits 1 naming the line",
          done.returncode == 1 and f"{journal} line 6 is not a journal entry" in done.stderr
          and done.stdout == "", done)
# A close-out takes its summary from the record, so it is the one writer that needs to read it.
journal = scratch_journal()
summarize(journal, "shipped the parser")
whole = journal.read_bytes()
journal.write_bytes(whole + b"not an entry\n")
done = run(journal, "close", "--handoff", "/handoffs/1.md")
check("close of a journal with a line that is not an entry exits 1 naming the line, and writes nothing",
      done.returncode == 1 and f"{journal} line 2 is not a journal entry" in done.stderr
      and journal.read_bytes() == whole + b"not an entry\n", done)
journal.write_bytes(whole)
done = run(journal, "close", "--handoff", "/handoffs/1.md")
check("and with the line removed, the close-out holds the summary recorded before it",
      done.returncode == 0 and entries(journal)[-1].summary == "shipped the parser", done)
blocked = SCRATCH / "a-file"
blocked.write_text("")
done = run(blocked / "journal.jsonl", "note", "nowhere to go")
check("a journal that cannot be written exits 1 with the reason",
      done.returncode == 1 and done.stderr.startswith("journal: "), done)
journal = scratch_journal()
(SCRATCH / "bytes.md").write_bytes(b"\xff\xfe")
for name, path in (("is not there", SCRATCH / "absent.md"), ("is not text", SCRATCH / "bytes.md"),
                   ("has no name", "")):
    for verb in ("note", "summary"):
        done = run(journal, verb, "--file", str(path))
        check(f"a {verb} whose file {name} exits 1 with the reason and writes nothing",
              done.returncode == 1 and done.stderr.startswith("journal: ")
              and not journal.exists(), done)

# A session that removed its worktree closes out from a directory that is no longer there.
gone = SCRATCH / "gone"
gone.mkdir()
journal = scratch_journal()
(SCRATCH / "removed.md").write_text("removed the worktree")
done = subprocess.run(
    ["sh", "-c", 'cd "$1" && rmdir "$1" && "$2" "$3" summary --file "$4" '
                 '&& exec "$2" "$3" close --handoff /handoffs/9.md',
     "sh", str(gone), sys.executable, CLI, str(SCRATCH / "removed.md")],
    text=True, capture_output=True,
    env=dict(os.environ, MEMENTO_JOURNAL=str(journal), CLAUDE_CODE_SESSION_ID="session-A"))
check("a summary and a close-out from a working directory since removed are recorded under the name "
      "the shell holds",
      done.returncode == 0 and [entry.cwd for entry in entries(journal)] == [str(gone)] * 2, done)

skill = open(os.path.join(os.path.dirname(HERE), "SKILL.md")).read()
check("the skill that documents it names it", "bin/journal" in skill)
for what, stated in (("a note's cap", f"{NOTE_CAP:,} characters"),
                     ("a summary's cap", f"{SUMMARY_CAP:,} characters"),
                     ("how many close-outs recall prints", f"the nearest {RECALLED},"),
                     ("the size of a recall", f"about {SUMMARY_CAP * RECALLED:,}")):
    check(f"the skill states {what} as the journal has it", stated in skill, stated)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
