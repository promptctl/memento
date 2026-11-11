#!/usr/bin/env python3
"""Tests for the journal: the chain a session's history is read along, what a session may recall of
it, the summary a close-out takes, and the record on disk. The chain, its readers and the summary
run in process over entries built here, with no file. The record runs against a scratch directory,
with real processes appending at once.

Run with: python3 journal.test.py
"""

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import journal  # noqa: E402
from journal import (NOTE_CAP, RECALLED, SUMMARY_CAP, Close, Note, Origin, Refused,  # noqa: E402
                     Unreadable, Wake, chain, closed, entries, entry_of, having, inside, line_of,
                     noted, origin, pending, recalled, record, shown, summarized, woke)

SCRATCH = Path(tempfile.mkdtemp(prefix="journal-test-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def by(session, ts="2026-11-10T00:00:00Z"):
    return Origin(ts, session, "/work", "trunk")


def handed(sessions):
    """The record a line of sessions leaves, each waking from the handoff the one before it wrote.
    A session writes a note, records the summary `did <session>`, then closes out with it."""
    kept = []
    for place, session in enumerate(sessions):
        if place:
            kept.append(woke(by(session), f"/handoffs/{place}.md"))
        kept.append(noted(by(session), f"note of {session}"))
        kept.append(summarized(by(session), f"did {session}"))
        kept.append(closed(by(session), f"/handoffs/{place + 1}.md", pending(kept, session)))
    return kept


def chained(kept, session):
    """The entries `show --chain` keeps for a session."""
    return [kept[at] for at in inside(kept, session, chain(kept, session)[0])]


def refusal(build, *args):
    try:
        build(*args)
    except Refused as refused:
        return str(refused)
    return None


def unreadable(read, *args):
    try:
        read(*args)
    except Unreadable as wrong:
        return str(wrong)
    return None


# --- the chain ---------------------------------------------------------------------------------

kept = handed("ABC")[:-3]  # C has woken and written nothing since.
links, end = chain(kept, "C")
check("a chain is followed from a session back through each handoff to the session that began it",
      [kept[at].summary for at in links] == ["did B", "did A"]
      and "no wake for session A" in end, (links, end))
lines = recalled(kept, "C")
check("recall prints the earlier sessions' summaries, oldest first",
      [line.strip() for line in lines if line.startswith("  ")] == ["did A", "did B"], lines)
check("recall never prints a note", not any("note of" in line for line in lines), lines)
check("recall says where the chain begins",
      lines[-1] == "Nothing earlier: the journal holds no wake for session A before the earliest "
                   "close-out shown.", lines)
check("a summary's header says when and where it was written",
      lines[0] == "2026-11-10T00:00:00Z  /work  (trunk)", lines[0])

lines = recalled(handed("A"), "A")
check("a session the journal holds no wake for has nothing to recall, and is told what is known",
      lines == ["Nothing earlier: the journal holds no wake for session A."], lines)

told = ["Nothing earlier: session B woke from /handoffs/old.md, which no close-out before it in "
        "the journal wrote."]
kept = [woke(by("B"), "/handoffs/old.md"), noted(by("B"), "n")]
lines = recalled(kept, "B")
check("a session woken by a handoff no close-out journaled is told which handoff", lines == told,
      lines)
lines = recalled(kept + [closed(by("A"), "/handoffs/old.md", "journaled late")], "B")
check("a close-out journaled after the wake it wrote for is not that wake's link", lines == told,
      lines)

kept = [closed(by(None), "/handoffs/1.md", "did it unnamed"), woke(by("B"), "/handoffs/1.md")]
lines = recalled(kept, "B")
check("a close-out that names no session is recalled, and the chain ends at it",
      "  did it unnamed" in lines and "names no session" in lines[-1], lines)

sessions = [f"S{n}" for n in range(RECALLED + 3)]
kept = handed(sessions)
lines = recalled(kept, sessions[-1])
check(f"recall shows the nearest {RECALLED} close-outs and counts the rest",
      [line.strip() for line in lines if line.startswith("  ")]
      == [f"did {session}" for session in sessions[2:-1]]
      and lines[-1] == "Earlier close-outs in this chain, not shown: 2.", lines)

# A session id that survives a reset: S closes out, and the session woken by that handoff is S again.
kept = [noted(by("S"), "before"), closed(by("S"), "/handoffs/1.md", "did the first stretch"),
        woke(by("S"), "/handoffs/1.md"), noted(by("S"), "after")]
check("a session id that survives a reset recalls its own earlier close-out once",
      recalled(kept, "S")[1:] == ["  did the first stretch", "",
                                  "Nothing earlier: the journal holds no wake for session S before "
                                  "the earliest close-out shown."],
      recalled(kept, "S"))
check("and its chain is everything it wrote on both sides of the reset", chained(kept, "S") == kept,
      chained(kept, "S"))

# A close-out whose transport refused, then the retry: two closes, and the successor woke from the second.
kept = [closed(by("A"), "/handoffs/1.md", "first try"), closed(by("A"), "/handoffs/2.md", "the retry"),
        woke(by("B"), "/handoffs/2.md")]
lines = recalled(kept, "B")
check("only the close-out whose handoff woke the successor is recalled",
      "  the retry" in lines and "  first try" not in lines, lines)

# One handoff delivered to two sessions: each follows it back.
kept = [closed(by("A"), "/handoffs/1.md", "did A"), woke(by("B"), "/handoffs/1.md"),
        woke(by("C"), "/handoffs/1.md")]
check("a handoff delivered twice is the predecessor of both sessions it woke",
      all("  did A" in recalled(kept, session) for session in "BC"), kept)

# A second handoff pasted into a live session: its newest wake is the one followed.
kept = [closed(by("A"), "/handoffs/1.md", "did A"), closed(by("X"), "/handoffs/9.md", "did X"),
        woke(by("B"), "/handoffs/1.md"), woke(by("B"), "/handoffs/9.md")]
check("a session woken twice follows its newest wake",
      "  did X" in recalled(kept, "B") and "  did A" not in recalled(kept, "B"), recalled(kept, "B"))

# A handoff path written twice (the same second, a recycled pid): the close nearest before the wake.
kept = [closed(by("A"), "/handoffs/1.md", "older"), closed(by("Z"), "/handoffs/1.md", "newer"),
        woke(by("B"), "/handoffs/1.md")]
check("of two close-outs naming one handoff, the one nearest before the wake is the predecessor",
      "  newer" in recalled(kept, "B") and "  older" not in recalled(kept, "B"), recalled(kept, "B"))

# --- the summary a close-out takes --------------------------------------------------------------

kept = [summarized(by("A"), "a first draft"), summarized(by("B"), "did B"), noted(by("A"), "n"),
        summarized(by("A"), "did A")]
check("a close-out takes the summary its session recorded, the later of two",
      (pending(kept, "A"), pending(kept, "B")) == ("did A", "did B"))
told = refusal(pending, kept, "C") or ""
check("a session that recorded none is refused, and told what a summary is and how to record one",
      "what the session did" in told and "journal summary --file <path>" in told
      and "last close-out" not in told, told)
kept.append(closed(by("A"), "/handoffs/1.md", pending(kept, "A")))
told = refusal(pending, kept, "A") or ""
check("a summary is taken by one close-out, and the next is told the last one took it",
      "since its last close-out" in told and "journal summary --file <path>" in told, told)
check("and another session's summary is still that session's to take", pending(kept, "B") == "did B")
# A session id that survives a reset: the successor's close-out takes what was recorded since.
kept += [woke(by("A"), "/handoffs/1.md"), summarized(by("A"), "did the second stretch")]
check("a session whose id survived a reset closes out with its own summary",
      pending(kept, "A") == "did the second stretch")

# An environment that names no session still closes out, with the summary recorded the same way.
kept = [summarized(by(None), "did it unnamed"), summarized(by("A"), "did A")]
check("a close-out that names no session takes the summary recorded with none",
      pending(kept, None) == "did it unnamed")
check("and never a named session's", refusal(pending, kept[1:], None) is not None)
check("a named session's close-out never takes a summary recorded with no session",
      refusal(pending, kept[:1], "A") is not None)

# --- what show keeps ----------------------------------------------------------------------------

kept = handed("AB") + [noted(by("elsewhere"), "another agent's note")]
check("a chain's entries are every entry its sessions wrote, and no other session's",
      chained(kept, "B") == kept[:-1], chained(kept, "B"))
check("a session the record has never seen has no entries",
      chained(kept, "nobody") == [], chained(kept, "nobody"))

# What a session wrote lies anywhere in the record, not only between its wake and its close-out.
kept = [noted(by("B"), "before either handoff"), closed(by("X"), "/handoffs/9.md", "did X"),
        noted(by("B"), "before the pasted handoff"), woke(by("B"), "/handoffs/9.md"),
        noted(by("B"), "after")]
check("a session woken mid-life keeps what it wrote before that wake", chained(kept, "B") == kept,
      chained(kept, "B"))
kept = [closed(by("A"), "/handoffs/1.md", "did A"), woke(by("B"), "/handoffs/1.md"),
        noted(by("B"), "between"), woke(by("B"), "/handoffs/1.md"), noted(by("B"), "after")]
check("a handoff delivered twice to one session keeps both wakes and the note between",
      chained(kept, "B") == kept, chained(kept, "B"))
kept = [closed(by("A"), "/handoffs/1.md", "did A"), noted(by("A"), "an afterthought"),
        woke(by("B"), "/handoffs/1.md")]
check("a note written after the close-out and before the successor woke is kept",
      chained(kept, "B") == kept, chained(kept, "B"))
kept = [noted(by(None), "nobody's note"), closed(by(None), "/handoffs/1.md", "did it unnamed"),
        woke(by("B"), "/handoffs/1.md")]
check("a close-out that names no session is in the chain alone, with no other unnamed entry",
      chained(kept, "B") == kept[1:], chained(kept, "B"))
note = noted(by("A"), "the parser\nloses a byte")
check("a field filter keeps an entry whose field contains the text",
      having(note, "text", "loses") and having(note, "kind", "note") and having(note, "session", "A"))
check("and drops one that lacks the field or the text",
      not having(note, "summary", "loses") and not having(note, "text", "gains"))
check("an absent value contains nothing",
      not having(noted(by(None), "n"), "session", "None") and not having(note, "session", "None"))
check("not even the empty text, which every value an entry does have contains",
      not having(note, "summary", "") and not having(noted(by(None), "n"), "session", "")
      and having(note, "text", ""))
check("an entry is shown with its origin and each field of its kind, later lines indented",
      shown(note) == "2026-11-10T00:00:00Z  note  A  /work  (trunk)\n"
                     "  text: the parser\n    loses a byte", shown(note))
check("a summary is shown with its text",
      shown(summarized(by("A"), "did it")) == "2026-11-10T00:00:00Z  summary  A  /work  (trunk)\n"
                                              "  summary: did it")
check("an entry that names no session is shown as naming none, not as a session called None",
      shown(noted(by(None), "n")).startswith("2026-11-10T00:00:00Z  note  (no session)  /work"),
      shown(noted(by(None), "n")))

# --- what the journal takes ---------------------------------------------------------------------

check("an empty note is refused", "note is empty" in (refusal(noted, by("A"), " \n ") or ""))
# A summary is held to one size where it is recorded and where a close-out holds it.
for what, summed in (("a summary", lambda text: summarized(by("A"), text)),
                     ("a close-out's summary", lambda text: closed(by("A"), "/h.md", text))):
    check(f"{what} that is empty is refused", "summary is empty" in (refusal(summed, " \n ") or ""))
    check(f"{what} over the cap is refused, with the cap named",
          f"{SUMMARY_CAP:,}" in (refusal(summed, "x" * (SUMMARY_CAP + 1)) or ""))
    check(f"{what} at the cap is taken", summed("x" * SUMMARY_CAP).summary == "x" * SUMMARY_CAP)
    check(f"{what} is kept without its surrounding whitespace",
          summed("\n did it \n").summary == "did it")
check("a note over its cap is refused, with the cap named",
      f"{NOTE_CAP:,}" in (refusal(noted, by("A"), "x" * (NOTE_CAP + 1)) or ""))
check("a note at its cap is taken", noted(by("A"), "x" * NOTE_CAP).text == "x" * NOTE_CAP)
# A wake finds its close-out by the handoff's path alone, so the path is the file's whole name.
for name, path in (("no", ""), ("a relative", "handoffs/1.md")):
    check(f"a close-out with {name} handoff path is refused",
          "not absolute" in (refusal(closed, by("A"), path, "did it") or ""))
    check(f"a wake with {name} handoff path is refused",
          "not absolute" in (refusal(woke, by("A"), path) or ""))

# --- an entry and its line ----------------------------------------------------------------------

awkward = "line one\nline two\u2028after a separator \"quoted\" \\ é"
for entry in (woke(by("A"), "/handoffs/a b.md"), noted(by(None), awkward),
              summarized(by("A"), awkward), closed(by("A"), "/h.md", awkward)):
    line = line_of(entry)
    check(f"a {entry.kind} entry reads back from its line as it was written",
          entry_of(line) == entry and type(entry_of(line)) is type(entry) and "\n" not in line, line)
for name, line in (("is not JSON", "{"), ("is not an object", "[1]"),
                   ("names no kind", '{"ts": "t"}'), ("names an unknown kind", '{"kind": "nap"}'),
                   ("lacks a field of its kind", line_of(noted(by("A"), "n")).replace('"text"', '"words"')),
                   ("carries a field its kind has not", line_of(woke(by("A"), "/h"))[:-1] + ', "text": "x"}'),
                   ("holds a number where text belongs",
                    line_of(noted(by("A"), "n")).replace('"text": "n"', '"text": 7')),
                   ("names no directory",
                    line_of(noted(by("A"), "n")).replace('"cwd": "/work"', '"cwd": null'))):
    check(f"a line that {name} is not an entry", unreadable(entry_of, line) is not None, line)

# --- the record on disk -------------------------------------------------------------------------

check("a journal nobody has written to holds no entries", entries(SCRATCH / "absent.jsonl") == [])

# The record's order is the order of writing, whatever the entries' own times say.
kept_at = SCRATCH / "made" / "on" / "first" / "write.jsonl"
written = [noted(by("A", "2026-11-01T00:00:00Z"), "written first, stamped later"),
           noted(by("A", "2026-10-31T23:59:59Z"), "written second, stamped earlier"),
           closed(by("A"), "/h.md", awkward)]
for entry in written:
    record(entry, kept_at)
check("the record reads back whole, in the order it was written", entries(kept_at) == written,
      entries(kept_at))

# A record whose last line lost its newline: a hand edit, or a writer killed mid-line.
cut = SCRATCH / "cut.jsonl"
cut.write_bytes(kept_at.read_bytes().rstrip(b"\n"))
record(written[0], cut)
check("an entry appended after an unterminated line starts a line of its own",
      entries(cut) == written + written[:1], unreadable(entries, cut))

for name, tail in (("is not JSON", b'{"kind": "note"\n'), ("is not text", b"\xff\xfe\n")):
    spoiled = SCRATCH / f"spoiled-{len(tail)}.jsonl"
    shutil.copy(kept_at, spoiled)
    with open(spoiled, "ab") as handle:
        handle.write(tail)
    wrong = unreadable(entries, spoiled) or ""
    check(f"a line that {name} is named by file and line",
          f"{spoiled} line 4 is not a journal entry" in wrong, wrong)

# Real processes appending at once, each line the longest a note makes.
directory = SCRATCH / "crowd.jsonl"
WRITERS, EACH = 8, 40
appending = ("import sys; sys.path.insert(0, sys.argv[1]); import journal; from pathlib import Path\n"
             "for n in range(int(sys.argv[4])):\n"
             "    journal.record(journal.noted(journal.Origin('2026-11-10T00:00:00Z', sys.argv[3],"
             " '/work', None), f'{n} '.ljust(journal.NOTE_CAP, 'x')), Path(sys.argv[2]))\n")
crowd = [subprocess.Popen([sys.executable, "-c", appending, LIB, str(directory), f"writer-{n}",
                           str(EACH)]) for n in range(WRITERS)]
exits = [writer.wait() for writer in crowd]
kept = entries(directory) if not any(exits) else []
check("entries appended by processes at once all arrive, none torn",
      len(kept) == WRITERS * EACH, (exits, len(kept)))
check("and each writer's entries stand in the order it wrote them",
      all([int(entry.text.split()[0]) for entry in kept if entry.session == f"writer-{n}"]
          == list(range(EACH)) for n in range(WRITERS)))

# --- where an entry comes from ------------------------------------------------------------------

repo, bare = SCRATCH / "repo", SCRATCH / "no-repo"
bare.mkdir()
quiet = dict(check=True, capture_output=True)
subprocess.run(["git", "init", "-q", str(repo)], **quiet)
subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/journal-lane"], cwd=repo, **quiet)
here = origin("session-1", str(repo))
check("an entry's origin carries the session, the directory and the branch checked out there",
      (here.session, here.cwd, here.branch) == ("session-1", str(repo), "journal-lane"), here)
check("its time is UTC to the second",
      re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", here.ts) is not None, here.ts)
check("outside a repository there is no branch", origin("session-1", str(bare)).branch is None)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
                "--allow-empty", "-m", "one"], cwd=repo, **quiet)
subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, **quiet)
check("on a detached HEAD there is no branch", origin("session-1", str(repo)).branch is None)
check("a directory that does not exist has no branch",
      origin("session-1", str(SCRATCH / "gone")).branch is None)
check("an environment that names no session leaves the session absent",
      origin(None, str(bare)).session is None and origin("", str(bare)).session is None)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
