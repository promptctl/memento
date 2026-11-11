#!/usr/bin/env python3
"""The journal: an append-only record of what each session woke from, noted, and closed out with.

A handoff carries the next step forward and nothing of what came before it. The journal keeps that:
a session's wake names the handoff it started from, its summary says what it did, its close names
the handoff it wrote and holds that summary, and notes are whatever it chose to deposit in between.
A session id changes at nearly every reset, so an agent's history is a chain of handoffs: a wake and
the close that wrote its handoff are the link from one session to the one before.

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
# A note is prose a person reads. Every read parses the whole record and no entry ever leaves it,
# so the cap is what keeps a log or a transcript out.
NOTE_CAP = 20_000

# When, by which session, and where an entry was written. `session` is None where the writer's
# environment named none, and `branch` is None where git names none for `cwd`.
Origin = collections.namedtuple("Origin", "ts session cwd branch")
ABSENT = ("session", "branch")


# [LAW:types-are-the-program] An entry is one of these four and carries exactly its kind's fields.
class Wake(collections.namedtuple("Wake", Origin._fields + ("handoff",))):
    """A session started from the handoff file at `handoff`."""
    __slots__ = ()
    kind = "wake"


class Note(collections.namedtuple("Note", Origin._fields + ("text",))):
    """Text a session chose to deposit."""
    __slots__ = ()
    kind = "note"


class Summary(collections.namedtuple("Summary", Origin._fields + ("summary",))):
    """What a session says it did, what came of it and why, for the close-out that follows."""
    __slots__ = ()
    kind = "summary"


class Close(collections.namedtuple("Close", Origin._fields + ("handoff", "summary"))):
    """A session wrote the handoff file at `handoff`, having done what `summary` says."""
    __slots__ = ()
    kind = "close"


KINDS = {shape.kind: shape for shape in (Wake, Note, Summary, Close)}
# Every name a `FIELD=TEXT` filter may use.
FIELDS = ("kind", *dict.fromkeys(field for shape in KINDS.values() for field in shape._fields))


class Refused(Exception):
    """Text the journal does not take; the message says why."""


class Unreadable(Exception):
    """A line of the record is not an entry; the message names the file and line."""


def _said(text, what, cap, instead):
    """A note's or a summary's text as the journal keeps it, without its surrounding whitespace.

    [LAW:single-enforcer] Every writer's text passes here, so this is where it is held to a size:
    not empty, and at most `cap` characters. `instead` says where longer text belongs."""
    kept = text.strip()
    if not kept:
        raise Refused(f"{what} is empty, so there is nothing to record.")
    if len(kept) > cap:
        raise Refused(f"{what} is {len(kept):,} characters and the most it may be is {cap:,}. "
                      f"{instead}")
    return kept


def _named(handoff):
    """A handoff file's path as the journal keeps it. A wake is joined to the close-out before it
    by this path alone, so it is the file's whole name: a relative path names a different file from
    each directory, and an empty one would join every session that gave none."""
    if not os.path.isabs(handoff):
        raise Refused(f"the handoff path {handoff!r} is not absolute. A wake finds the close-out "
                      f"before it by this path, so give the handoff file's full path.")
    return handoff


def noted(origin, text):
    """A note entry for this text. Raises Refused for text that is empty or over NOTE_CAP."""
    return Note(*origin, _said(text, "the note", NOTE_CAP,
                               "Leave a log or a transcript in a file of its own, and name that "
                               "file in the note."))


def _summed(text):
    """A summary's text as the journal keeps it: not empty and at most SUMMARY_CAP, the size later
    sessions read back."""
    return _said(text, "the summary", SUMMARY_CAP,
                 "Say what the session did and what came of it in a few sentences; the detail "
                 "belongs in notes.")


def summarized(origin, text):
    """A summary entry for this text. Raises Refused for text that is empty or over SUMMARY_CAP."""
    return Summary(*origin, _summed(text))


def closed(origin, handoff, summary):
    """A close entry for the handoff a session wrote. Raises Refused for a handoff path that is not
    absolute, and for a summary that is empty or over SUMMARY_CAP. Its writer records it before the
    handoff can wake anyone: a wake finds its close-out only earlier in the record."""
    return Close(*origin, _named(handoff), _summed(summary))


def woke(origin, handoff):
    """A wake entry for the handoff a session started from. Raises Refused for a handoff path that
    is not absolute."""
    return Wake(*origin, _named(handoff))


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

    [LAW:parse-dont-validate] A line comes back as a Wake, Note, Summary or Close, or this raises: a
    kind that is not one of the four, keys that are not exactly that kind's fields, or a value that
    is not text where only `session` and `branch` may be absent instead."""
    try:
        fields = json.loads(line)
        entry = KINDS[fields.pop("kind")](**fields)
        for field, value in entry._asdict().items():
            if not (isinstance(value, str) or value is None and field in ABSENT):
                raise TypeError(f"{field} is {value!r}, which is not text")
        return entry
    except (ValueError, KeyError, TypeError, AttributeError) as wrong:
        raise Unreadable(f"{type(wrong).__name__}: {wrong}") from wrong


def record(entry, path):
    """Appends one entry to the record.

    Sessions append concurrently, so the line goes in whole under an exclusive lock, and `entries`
    reads under a shared one.

    [LAW:single-enforcer] This is the only writer, so it is where an entry is given a line of its
    own: after a last line left unterminated, by a hand edit or a writer killed mid-line, the entry
    starts a new line instead of joining that one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(max(handle.seek(0, os.SEEK_END) - 1, 0))
        opening = b"" if handle.read(1) in (b"", b"\n") else b"\n"
        handle.write(opening + line_of(entry).encode() + b"\n")


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


def pending(entries, session):
    """The summary a close-out by this session takes: the one it recorded since its last close-out,
    the latest where it recorded several. Raises Refused where there is none.

    [LAW:one-source-of-truth] Whether a summary is still to be taken is read off the record's order
    and marked nowhere else: the close that follows a summary has taken it, so a session whose id
    survived a reset closes out with its own words and not its predecessor's.

    A session is the id its entries carry. Entries that carry none are one session here, and here
    only: a close-out must not fail because its environment names no session, so the summary it
    takes is the latest recorded the same way."""
    at = _latest(entries, len(entries),
                 lambda entry: isinstance(entry, (Summary, Close)) and entry.session == session)
    if at is None or isinstance(entries[at], Close):
        raise Refused(
            "this session has recorded no summary for a close-out to take"
            + ("" if at is None else " since its last close-out, which took the one before it")
            + f". Write what the session did, what came of it and why, in at most {SUMMARY_CAP:,} "
              "characters, to a file with the Write tool, and record it before closing out: "
              "journal summary --file <path>")
    return entries[at].summary


def chain(entries, session):
    """Follows a session's chain of handoffs back through the record.

    Returns the positions of the close-outs that link the chain, nearest first, and a sentence
    saying why the chain goes no further. A link is found by position, not by session id alone: the
    session's latest wake before the link that follows, then the latest close before that wake
    naming the same handoff, and on from the session that wrote it. Each step lands earlier in the
    record, so the walk ends even where a session id survived a reset or a handoff was delivered
    twice. The sentence says what the record holds and no more: a session it holds no wake for may
    have started fresh, or woken where nothing journaled it."""
    links, until = [], len(entries)
    while True:
        woken = _latest(entries, until,
                        lambda entry: isinstance(entry, Wake) and entry.session == session)
        if woken is None:
            return links, (f"the journal holds no wake for session {session}"
                           + (" before the earliest close-out shown." if links else "."))
        handoff = entries[woken].handoff
        wrote = _latest(entries, woken,
                        lambda entry: isinstance(entry, Close) and entry.handoff == handoff)
        if wrote is None:
            return links, (f"session {session} woke from {handoff}, which no close-out before it in "
                           f"the journal wrote.")
        links.append(wrote)
        session, until = entries[wrote].session, wrote + 1
        if session is None:
            return links, ("the earliest close-out shown names no session, so the chain cannot be "
                           "followed past it.")


def inside(entries, session, links):
    """The positions of a chain's entries, in record order: every entry written by the session the
    chain starts from or by a session that wrote one of its links, wherever in the record it lies.
    A link that names no session is in the chain alone, since an absent session is nobody whose
    other entries could be told from anyone else's."""
    sessions = {session, *(entries[at].session for at in links)} - {None}
    linked = set(links)
    return [at for at, entry in enumerate(entries) if entry.session in sessions or at in linked]


def having(entry, field, text):
    """Whether an entry has the field and its value contains the text. An absent value contains
    nothing, the empty text included, so `summary=` keeps the entries that have a summary."""
    value = getattr(entry, field, None)
    return value is not None and text in value


def _where(entry):
    return entry.cwd + (f"  ({entry.branch})" if entry.branch else "")


def _indented(text, by):
    return text.replace("\n", "\n" + " " * by)


def recalled(entries, session):
    """What a session may read of its own history, as lines: the summaries of the close-outs that
    link its chain, the nearest RECALLED of them, oldest first, then how many earlier ones there are
    or why there are none. Never a note."""
    links, end = chain(entries, session)
    near = [entries[at] for at in links[:RECALLED]][::-1]
    earlier = len(links) - len(near)
    return [line for close in near
            for line in (f"{close.ts}  {_where(close)}", "  " + _indented(close.summary, 2), "")
            ] + [f"Earlier close-outs in this chain, not shown: {earlier}." if earlier
                 else f"Nothing earlier: {end}"]


def shown(entry):
    """One entry in full, as the user reads it: its origin on one line, then each field of its kind."""
    return "\n".join([f"{entry.ts}  {entry.kind}  {entry.session or '(no session)'}  {_where(entry)}"]
                     + [f"  {field}: {_indented(getattr(entry, field), 4)}"
                        for field in entry._fields[len(Origin._fields):]])
