#!/usr/bin/env python3
"""The journal: an append-only record of what each session woke from, noted, and closed out with.

A handoff carries the next step forward and nothing of what came before it. The journal keeps that:
a session's wake names the handoff it started from, its close names the handoff it wrote and
summarizes what it did, and notes are whatever it chose to deposit in between. A session id changes
at nearly every reset, so an agent's history is a chain of handoffs: a wake and the close that wrote
its handoff are the link from one session to the one before.

The record is one file, one entry per line. An entry's place in it is the only order anything here
relies on: the timestamp an entry carries is for a person to read.
"""

import collections
import fcntl
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

JOURNAL = Path(os.environ.get("MEMENTO_JOURNAL")
               or Path.home() / ".claude" / "memento" / "journal.jsonl")
# A summary is what later sessions read back, RECALLED of them at a time, so its size is the read's.
SUMMARY_CAP = 1200
RECALLED = 5

# When, by which session, and where an entry was written. `session` is None where the writer's
# environment named none, and `branch` is None where git names none for `cwd`.
Origin = collections.namedtuple("Origin", "ts session cwd branch")


# [LAW:types-are-the-program] An entry is one of these three and carries exactly its kind's fields.
class Wake(collections.namedtuple("Wake", Origin._fields + ("handoff",))):
    """A session started from the handoff file at `handoff`."""
    __slots__ = ()
    kind = "wake"


class Note(collections.namedtuple("Note", Origin._fields + ("text",))):
    """Text a session chose to deposit."""
    __slots__ = ()
    kind = "note"


class Close(collections.namedtuple("Close", Origin._fields + ("handoff", "summary"))):
    """A session wrote the handoff file at `handoff`, having done what `summary` says."""
    __slots__ = ()
    kind = "close"


KINDS = {shape.kind: shape for shape in (Wake, Note, Close)}
# Every name a `FIELD=TEXT` filter may use.
FIELDS = ("kind", *dict.fromkeys(field for shape in KINDS.values() for field in shape._fields))

# One session's stretch of a chain: its entries are those it wrote at positions [since, until).
Hop = collections.namedtuple("Hop", "session since until")


class Refused(Exception):
    """Text the journal does not take; the message says why."""


class Unreadable(Exception):
    """A line of the record is not an entry; the message names the file and line."""


def _said(text, what):
    stripped = text.strip()
    if not stripped:
        raise Refused(f"{what} is empty, so there is nothing to record.")
    return stripped


def noted(origin, text):
    """A note entry for this text. Raises Refused for text that is empty."""
    return Note(*origin, _said(text, "the note"))


def closed(origin, handoff, summary):
    """A close entry for the handoff a session wrote.

    [LAW:single-enforcer] Every close-out's summary passes here, so this is where it is held to a
    size later sessions can read back: not empty, and at most SUMMARY_CAP characters."""
    kept = _said(summary, "the summary")
    if len(kept) > SUMMARY_CAP:
        raise Refused(f"the summary is {len(kept):,} characters and the most it may be is "
                      f"{SUMMARY_CAP:,}. Say what the session did and what came of it in a few "
                      f"sentences; the detail belongs in notes.")
    return Close(*origin, handoff, kept)


def woke(origin, handoff):
    """A wake entry for the handoff a session started from."""
    return Wake(*origin, handoff)


def branch_at(cwd):
    """The branch checked out at a directory, or None where git names none: outside a repository,
    on a detached HEAD, or with no git to ask."""
    try:
        asked = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=cwd,
                               capture_output=True, text=True)
    except OSError:
        return None
    return asked.stdout.strip() or None


def origin(session, cwd):
    """The origin of an entry written now, by this session, from this directory."""
    return Origin(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), session or None, cwd,
                  branch_at(cwd))


def line_of(entry):
    """One entry as its line of the record, without the newline."""
    return json.dumps({"kind": entry.kind, **entry._asdict()}, ensure_ascii=False)


def entry_of(line):
    """The entry one line of the record holds.

    [LAW:parse-dont-validate] A line comes back as a Wake, Note or Close, or this raises: a kind
    that is not one of the three, or keys that are not exactly that kind's fields."""
    try:
        fields = json.loads(line)
        return KINDS[fields.pop("kind")](**fields)
    except (ValueError, KeyError, TypeError, AttributeError) as wrong:
        raise Unreadable(f"{type(wrong).__name__}: {wrong}") from wrong


def record(entry, path):
    """Appends one entry to the record.

    Sessions append concurrently, so the line goes in whole under an exclusive lock, and `entries`
    reads under a shared one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write((line_of(entry) + "\n").encode())


def entries(path):
    """The whole record, oldest entry first. A journal nobody has written to yet holds none."""
    try:
        with open(path, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            lines = handle.read().split(b"\n")
    except FileNotFoundError:
        return []
    found = []
    for number, line in enumerate(lines, 1):
        if line:
            try:
                found.append(entry_of(line))
            except Unreadable as wrong:
                raise Unreadable(f"{path} line {number} is not a journal entry ({wrong}). "
                                 f"Fix it or remove it.") from wrong
    return found


def _latest(entries, before, wanted):
    """The position of the last entry before `before` that `wanted` accepts, or None."""
    return next((at for at in range(before - 1, -1, -1) if wanted(entries[at])), None)


def chain(entries, session):
    """Follows a session's chain of handoffs back through the record.

    Returns the hops, newest first, and a sentence saying why the chain goes no further. Each hop
    but the newest ends at the close-out whose handoff woke the hop after it. A hop is found by
    position, not by session id alone: the session's latest wake before the hop that follows, then
    the latest close before that wake naming the same handoff. Each step lands earlier in the
    record, so the walk ends even where a session id survived a reset or a handoff was delivered
    twice."""
    hops, until = [], len(entries)
    while True:
        woken = _latest(entries, until,
                        lambda entry: isinstance(entry, Wake) and entry.session == session)
        if woken is None:
            return hops + [Hop(session, 0, until)], f"session {session} did not wake from a handoff."
        hops.append(Hop(session, woken, until))
        handoff = entries[woken].handoff
        wrote = _latest(entries, woken,
                        lambda entry: isinstance(entry, Close) and entry.handoff == handoff)
        if wrote is None:
            return hops, (f"session {session} woke from {handoff}, which no journaled close-out "
                          f"wrote.")
        session, until = entries[wrote].session, wrote + 1
        if session is None:
            return hops + [Hop(None, wrote, until)], ("the earliest close-out shown names no "
                                                      "session, so the chain cannot be followed "
                                                      "past it.")


def inside(entries, hops):
    """The positions of the entries the hops cover, in record order."""
    return sorted(at for hop in hops for at in range(hop.since, hop.until)
                  if entries[at].session == hop.session)


def having(entry, field, text):
    """Whether an entry has the field and its value contains the text. An absent value contains nothing."""
    return text in (getattr(entry, field, None) or "")


def _where(entry):
    return entry.cwd + (f"  ({entry.branch})" if entry.branch else "")


def _indented(text, by):
    return text.replace("\n", "\n" + " " * by)


def recalled(entries, session):
    """What a session may read of its own history, as lines: the summaries of the close-outs that
    link its chain, the nearest RECALLED of them, oldest first, then how many earlier ones there are
    or why there are none. Never a note."""
    hops, end = chain(entries, session)
    links = [entries[hop.until - 1] for hop in hops[1:]]
    near = links[:RECALLED][::-1]
    earlier = len(links) - len(near)
    return [line for close in near
            for line in (f"{close.ts}  {_where(close)}", "  " + _indented(close.summary, 2), "")
            ] + [f"Earlier close-outs in this chain, not shown: {earlier}." if earlier
                 else f"Nothing earlier: {end}"]


def shown(entry):
    """One entry in full, as the user reads it: its origin on one line, then each field of its kind."""
    return "\n".join([f"{entry.ts}  {entry.kind}  {entry.session}  {_where(entry)}"]
                     + [f"  {field}: {_indented(getattr(entry, field), 4)}"
                        for field in entry._fields[len(Origin._fields):]])
