#!/usr/bin/env python3
"""The config layers that set the context ceiling: one file format, read and written in one place.

Two programs decide a ceiling and they have to agree about it - the Stop hook that reads the
layers to gate a session, and the `ceiling` command that writes them to move one. A second
implementation of this grammar is the divergence [LAW:one-source-of-truth] forbids, and what it
produces is not a ceiling that failed to move: a value the reader here rejects stops the hook,
which Claude Code treats as non-blocking, so the gate silently stops running for the session
whose file holds it. `write_ceiling` emits only what `ceiling_in` accepts and reads back what it
wrote, so the writer cannot guess the grammar wrong - there is nothing left to guess.

The failure arm is the process. A file that is not this format exits 1 naming the file and the
line, which is what both callers want at the moment a ceiling is unreadable: a ceiling you
believe you set and did not is worse than no ceiling. [LAW:no-silent-failure]
"""

import collections
import math
import os
import re
import sys
from pathlib import Path

DEFAULT_CEILING = 250_000
# One filename at every layer, so a second setting is a new key rather than a new file, a new
# lookup and a new precedence chain. [LAW:composability]
CONFIG_NAME = "memento.conf"
# A repo carries its own config as a dot-directory, because a checkout has no XDG anything.
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CONFIG_HOME = Path(os.environ.get("MEMENTO_CONFIG_HOME") or XDG_CONFIG / "promptctl")
USER_CONFIG = CONFIG_HOME / CONFIG_NAME
SESSION_CONFIGS = CONFIG_HOME / "sessions"
PROJECT_CONFIG_DIR = ".promptctl"
# What the shared layers resolved to when a session started, in the same `key = value` shape
# every other layer uses, so one parser reads them all. The hook writes this file and the agent
# writes CONFIG_NAME beside it: one writer each, which is what keeps two files in one directory
# from being two clocks. [LAW:one-source-of-truth] The name says what it holds rather than what
# it sets, because a file called `shared.conf` invites the hand-edit that would defeat it.
SHARED_AT_START = "shared-at-start.conf"
CEILING_KEY = "ceiling"
DISABLING_WORD = "off"
# [0-9] rather than \d: `str.isdigit` was true of characters `int` then refused, so the guard
# and the conversion disagreed. Underscores group digits as Python's own literals do.
CEILING_RE = re.compile(r"(?P<sign>[+-]?)(?P<digits>[0-9]+(?:_[0-9]+)*)\Z")
# A ceiling as one layer wrote it, carrying the file and line a person goes to change it.
Written = collections.namedtuple("Written", "source text")


def ceiling_in(path):
    """The ceiling one config file sets, or None. [LAW:no-silent-failure] a line that is not one
    exits here: a key that reads as a no-op is precisely the ceiling its author believes they
    set and did not."""
    found = None
    for number, line in enumerate(path.read_text().splitlines() if path.exists() else [], 1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        key, assigned, text = (part.strip() for part in stripped.partition("="))
        if not assigned or not text:
            sys.exit(f"memento config: {path} line {number} should read `key = value`, "
                     f"but reads {line.strip()!r}. Fix it or remove it.")
        if key != CEILING_KEY:
            sys.exit(f"memento config: {path} line {number} sets {key!r}, which memento has "
                     f"no such setting for. It reads: {CEILING_KEY}.")
        if found:
            sys.exit(f"memento config: {path} sets {key!r} twice, at {found.source} and "
                     f"line {number}. Keep the one you meant.")
        found = Written(f"{path} line {number}", text)
    return found


def project_file(anchor):
    """The project config in force at or above a directory, or None.

    The walk stops at the first file it finds, so one file at a repo root reaches every
    subdirectory and every worktree nested under it. Which file is in force is asked separately
    from what it says, because a writer needs the path and a reader needs the ceiling, and a
    second walk for the writer is a second answer to one question. [LAW:one-source-of-truth] the
    user's own file is passed over where the walk finds it, because applying one file as two
    layers is the divergence that law exists to forbid."""
    start = Path(anchor).resolve()
    for directory in (start, *start.parents):
        candidate = directory / PROJECT_CONFIG_DIR / CONFIG_NAME
        if candidate.exists() and candidate.resolve() != USER_CONFIG.resolve():
            return candidate
    return None


def project_ceiling(anchor):
    """The ceiling the project config in force sets, or None where no project sets one."""
    found = project_file(anchor)
    return ceiling_in(found) if found else None


def parse_ceiling(written):
    """One written ceiling, as the move it makes on the ceiling beneath it. The three things a
    person can write - a count, an adjustment, `off` - leave here as one thing, so the fold
    applies them in order with nothing left to dispatch on. [LAW:dataflow-not-control-flow]"""
    if written.text.lower() == DISABLING_WORD:
        return lambda beneath: math.inf
    shape = CEILING_RE.match(written.text)
    if not shape:
        sys.exit(f"memento config: {written.source} should hold a number of tokens, a signed "
                 f"adjustment like +100_000, or {DISABLING_WORD}, but reads "
                 f"{written.text!r}. Fix it or remove it.")
    magnitude = int(shape.group("digits").replace("_", ""))
    if not shape.group("sign"):
        return lambda beneath: magnitude
    moved = magnitude if shape.group("sign") == "+" else -magnitude
    return lambda beneath: beneath + moved


def render(ceiling):
    """One resolved ceiling as the text a config file holds for it.

    The inverse of `parse_ceiling`'s absolute arm, and the reason a number and a written layer
    are never two vocabularies: anything this renders, `ceiling_in` reads back unchanged."""
    return DISABLING_WORD if ceiling == math.inf else str(ceiling)


def session_directory(session_id):
    """The directory holding one session's files, for a session id that names one directory and
    nothing else.

    [LAW:parse-dont-validate] an id that is not a bare name reads a config from outside the tree
    - `Path.__truediv__` discards the left operand when the right is absolute, and follows `..`
    when it is not. Containment is asked of the resolved directory rather than of the spelling,
    because `..`, `./..` and `..//` all name the same place and such spellings do not form a
    list. [LAW:single-enforcer] every one of the session's files hangs off this one result, so an
    id becomes a path exactly once however many files a session grows."""
    directory = (SESSION_CONFIGS / str(session_id)).resolve()
    if directory.parent != SESSION_CONFIGS.resolve():
        sys.exit(f"memento config: session id {session_id!r} names {directory}, which is not "
                 f"a session directory under {SESSION_CONFIGS}. Memento cannot tell which "
                 f"session's settings it was meant to read.")
    return directory


def folded(layers, beneath=DEFAULT_CEILING):
    """Every written layer's move applied to the ceiling beneath it, in order.

    [LAW:single-enforcer] a resolved ceiling is checked for sense here, where every fold passes,
    rather than at one of them. The shared fold is the one that gets written down, and a negative
    reaching the record is unrecoverable: `-50000` is written, read back as an *adjustment*, and
    resolves to 200,000 - a positive ceiling nobody set, in place of the loud exit. The format
    cannot express a negative absolute and is never asked to, because no layer may resolve to
    one."""
    ceiling = beneath
    for setting in layers:
        ceiling = parse_ceiling(setting)(ceiling)
    if ceiling < 0:
        sys.exit(f"memento config: a ceiling of {ceiling:,} tokens is set by "
                 f"{', then '.join(one.source for one in layers)}, and a count of tokens is "
                 f"never negative.")
    return ceiling


def live_shared(anchor):
    """The user and project layers folded as they stand right now. This is the ceiling a session
    starting here would begin under, and the only reader that wants it mid-session is one asking
    what a change to those layers would mean for the next session rather than for this one."""
    return folded([one for one in (ceiling_in(USER_CONFIG), project_ceiling(anchor)) if one])


def write_ceiling(path, ceiling):
    """Replace one config file with the ceiling it now sets, and hand back what that file says.

    [LAW:effects-at-boundaries] the one place a ceiling becomes a file. Written whole and moved
    into place, because a create-then-write leaves the file empty for the width of one flush: a
    session killed inside that window comes back to a file that exists and parses to nothing,
    which no later stop can complete and every later stop dies on - the gate off for that
    session, permanently, with nothing in the log to say so. After `os.replace` the file is
    absent or complete, and never the third thing.

    Read back rather than handed back, so the writer and every later reader quote one file rather
    than two spellings of it, and so a value this wrote that `ceiling_in` would refuse fails here
    on the write rather than on the next stop that reads it. [LAW:one-source-of-truth] that
    read-back is also what leaves a writer nothing to guess: the only text that reaches disk is
    text the reader above accepts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}")
    partial.write_text(f"{CEILING_KEY} = {render(ceiling)}\n")
    os.replace(partial, path)
    return ceiling_in(path)


def shared_at_start(path, anchor):
    """What the user and project layers resolved to when this session started, recording it the
    first time it is asked for.

    The ceiling a session runs under is a fact about that session, so it is held as state the
    session owns rather than re-derived at every stop from files a stranger edits mid-run.
    [LAW:no-ambient-temporal-coupling] the alternative - rank each layer's mtime against the
    session's start - cannot see the change that caused this ticket: on 2026-09-06 a shared line
    was *deleted*, and a file that no longer exists has no mtime to rank. Freezing the resolved
    value takes an edit, a deletion, a whole new layer and a file rewritten into a syntax error
    as one case, with no direction test and no raise-or-lower asymmetry, because once this file
    exists the shared ones are never opened again.

    The record is made at the session's first stop rather than at its first token, because a
    stop is the only event the hook is given. That is one turn of drift, spent where a session
    is still far below any ceiling. Nothing here overwrites a record that already stands: the
    write is reached only for a path `ceiling_in` read nothing from."""
    return ceiling_in(path) or write_ceiling(path, live_shared(anchor))


def shared_unrecorded(path, anchor):
    """What this session's shared layers contribute, read without making the record.

    The hook owns that record - one writer, which is what keeps two files in one directory from
    being two clocks - so a reader that only wants the number takes the value the record would
    have held instead of creating it. Creating it here would be worse than untidy: a project
    ceiling written in the same breath would be frozen into the record as the session's shared
    base and then applied a second time as the session's own layer, landing the session at a
    number twice the headroom anyone asked for."""
    return ceiling_in(path) or Written(f"the user and project layers above {anchor}",
                                       render(live_shared(anchor)))


def in_force(directory, shared):
    """The ceiling in force for one session: the shared layers as that session holds them, moved
    by the session's own layer as it stands right now.

    [LAW:single-enforcer] the one place the order between the layers is decided, so the hook that
    gates a session on its ceiling and the command that moves one cannot disagree about which
    layer wins."""
    return folded([one for one in (shared, ceiling_in(directory / CONFIG_NAME)) if one])
