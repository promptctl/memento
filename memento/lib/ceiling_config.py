#!/usr/bin/env python3
"""The config layers that set the context ceiling: one file format, read and written in one place.

Two programs decide a ceiling and they have to agree about it - the Stop hook that reads the
layers to gate a session, and the `ceiling` command that writes them to move one. A second
implementation of this grammar is the divergence [LAW:one-source-of-truth] forbids, and what it
produces is not a ceiling that failed to move: a value the reader here rejects stops the hook,
which Claude Code treats as non-blocking, so the gate silently stops running for the session
whose file holds it. `write_ceiling` emits only what `ceiling_in` accepts and reads back what it
wrote, so the writer cannot guess the grammar wrong - there is nothing left to guess.

The failure arm is the process. A file that is not this format exits 1 naming the file, and the
line wherever there is a line to name, which is what both callers want at the moment a ceiling is
unreadable: a ceiling you believe you set and did not is worse than no ceiling.
[LAW:no-silent-failure]
"""

import collections
import contextlib
import functools
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path

DEFAULT_CEILING = 350_000
# How far past its ceiling a session may run to finish the unit of work it is in the middle of.
# A close-out forced mid-unit hands the next session a half-done task to reread from scratch, so the
# ceiling is where a close-out falls due at the next unit boundary and the ceiling plus this is
# where it is due regardless. A distance rather than a second number, so every layer that moves the
# ceiling moves the limit with it and the two cannot be set into disagreeing.
# [LAW:one-source-of-truth]
GRACE = 100_000
# One filename at every layer, so a second setting is a new key rather than a new file, a new
# lookup and a new precedence chain. [LAW:composability]
CONFIG_NAME = "memento.conf"
# A repo carries its own config as a dot-directory, because a checkout has no XDG anything.
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CONFIG_HOME = Path(os.environ.get("MEMENTO_CONFIG_HOME") or XDG_CONFIG / "promptctl")
USER_CONFIG = CONFIG_HOME / CONFIG_NAME
SESSION_CONFIGS = CONFIG_HOME / "sessions"
# How long a session directory may go unseen before the sweep removes it. A session's record is
# touched at every stop (`mark_seen`), so this measures time since its last stop, not since it
# started: a session that keeps stopping within the cutoff keeps its record young and cannot be swept.
# A session that goes unseen for longer is finished for this purpose: the next new session's sweep
# removes its directory while it stays dormant, and should it ever resume it re-reads the shared layers
# - for a frozen value that stale, the correct answer, not the staleness the record exists to prevent.
# (A pane that instead keeps stopping refreshes its own record and stays; the value it holds frozen
# across a long resume is value staleness, a separate concern from this growth bound.)
# [LAW:parse-dont-validate] Set comfortably beyond any real resume, so removal falls only on sessions
# for which re-reading is right.
STALE_SESSION_AGE_SECONDS = 30 * 24 * 60 * 60
PROJECT_CONFIG_DIR = ".promptctl"
# What the shared layers resolved to when a session started, in the same `key = value` shape
# every other layer uses, so one parser reads them all. The hook writes this file and the agent
# writes CONFIG_NAME beside it: one writer each, which is what keeps two files in one directory
# from being two clocks. [LAW:one-source-of-truth] The name says what it holds rather than what
# it sets, because a file called `shared.conf` invites the hand-edit that would defeat it.
SHARED_AT_START = "shared-at-start.conf"
# The context size a session measured when its close-out was credited, kept beside the record so a
# later stop can tell a reset that landed from one that was scheduled and never did. The tmux
# transport resets a session in place, keeping its id and so its record, which would otherwise freeze
# the first context's shared ceiling onto every context after it. A later stop whose context has
# fallen below this number is running the reset's fresh, smaller context and re-derives; one that has
# not is still running the original context and must keep the value it froze. The name says what its
# presence means - a reset awaiting confirmation - rather than what it holds. [LAW:one-source-of-truth]
RESET_PENDING = "reset-pending"
CEILING_KEY = "ceiling"
DISABLING_WORD = "off"
PROJECT_VARIABLE = "CLAUDE_PROJECT_DIR"
# [0-9] rather than \d: `str.isdigit` was true of characters `int` then refused, so the guard
# and the conversion disagreed. Underscores group digits as Python's own literals do.
CEILING_RE = re.compile(r"(?P<sign>[+-]?)(?P<digits>[0-9]+(?:_[0-9]+)*)\Z")
# A ceiling as one layer wrote it, carrying the file and line a person goes to change it.
Written = collections.namedtuple("Written", "source text")


def lines_in(path):
    """One config file's lines, and none for a path no file stands at.

    A file of bytes that are not text is a file that is not this format, answered here rather than
    left to each caller, because it is the same judgement `ceiling_in` makes about every other shape
    that is not this format and there is one place that judgement belongs. [LAW:single-enforcer] The
    hook reads its layers through here too, and a traceback out of a Stop hook is a gate Claude Code
    treats as non-blocking - off, for a reason nothing states. [LAW:no-silent-failure]

    What the filesystem refuses is deliberately not caught: no permission and no such device are not
    about the format, and the caller that can act on one - the command about to remove the file - is
    the one that catches it.

    The one refusal that IS caught is the file vanishing between the check and the read: `sweep_sessions`
    can now delete another session's directory while that session's own hook is mid-read, so the
    exists-then-read here has a real race. A file that is gone reads as absent - the same answer the
    `exists()` check above gives - rather than a traceback that would take the reader's gate down for a
    reason nothing states. [LAW:no-silent-failure]"""
    if not path.exists():
        return []
    try:
        return path.read_text().splitlines()
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as refusal:
        sys.exit(f"memento config: {path} holds bytes that are not text, so no line of it can set "
                 f"a ceiling: {refusal}. Fix it or remove it.")


def ceiling_in(path):
    """The ceiling one config file sets, or None. [LAW:no-silent-failure] a line that is not one
    exits here: a key that reads as a no-op is precisely the ceiling its author believes they
    set and did not."""
    found = None
    for number, line in enumerate(lines_in(path), 1):
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


def anchored(fallback):
    """The directory the project layer is looked up from, for a caller that knows where it would
    stand if the session named no project.

    The hook is handed a directory by Claude Code and this command has only its own, which is the
    whole of what differs between them - so that is the argument, and the part that must not differ
    is here. [LAW:one-source-of-truth] a ceiling that moved because something ran `cd` would be a
    ceiling nobody set, and two copies of this line agreeing today is not the same as one line."""
    return os.environ.get(PROJECT_VARIABLE) or fallback


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
    applies them in order with nothing left to dispatch on. [LAW:dataflow-not-control-flow]

    What is beneath arrives as a thunk, and only the arm that has a use for it calls one: a count
    and `off` state a ceiling outright, so a caller replacing a layer with either of them never has
    to read the layer it is replacing - which matters because reading it can fail. The arms already
    differ in whether the number beneath is load-bearing; this is that difference made true of the
    work as well as of the arithmetic."""
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
    return lambda beneath: beneath() + moved


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


def mark_seen(record):
    """Refresh a session record's mtime, so its age measures the time since this session's last stop
    rather than since it started.

    [LAW:effects-at-boundaries] the sweep reads this mtime as the session's sign of life, so a session
    that keeps stopping keeps its record younger than the cutoff and cannot be swept - the freeze holds
    for exactly as long as the session is still stopping. (A session that goes dormant past the cutoff
    is reaped while dormant by the next new session's sweep; if it later resumes it re-reads the shared
    layers, which for a value that stale is correct.)
    Best-effort, for the same reason `log` is: this is bookkeeping the sweep consumes, not the gate, so
    a failure to touch is reported and never fatal - raising here would take the whole gate down with
    the bookkeeping. [LAW:no-silent-failure]"""
    try:
        os.utime(record)
    except OSError as failure:
        print(f"memento config: cannot refresh {record}: {failure}", file=sys.stderr)


def _last_seen(entry):
    """When anything last happened in a session directory - the newest mtime among the directory and
    everything in it - or, for a stray file, its own mtime.

    A stop touches the record, the `ceiling` command writes the session's own layer, and either write
    stages a `.<pid>` partial before it lands; each of those is a real event at a real time, so the
    newest mtime in the directory is the session's last sign of life. A partial counts too, and must:
    a fresh one is a write still in flight, and a directory reaped out from under it would fail that
    write. An abandoned partial is simply old, and ages out with everything else a full cutoff after
    the write that left it - which is when that write, the directory's last activity, actually happened.
    So there is nothing to special-case: the newest mtime is the answer either way."""
    if entry.is_dir():
        return max([entry.stat().st_mtime] + [child.stat().st_mtime for child in entry.iterdir()])
    return entry.stat().st_mtime


def sweep_sessions(keep):
    """Remove every finished session's directory from the sessions tree, and with it the stray
    `.<pid>` partial a killed record write leaves behind.

    A finished session's directory can hold more than the record: a per-session ceiling the user set
    with the `ceiling` command lives beside it as CONFIG_NAME, and it is removed too. That is intended,
    not a leak - `_last_seen` counts every file's mtime, so a session layer written recently keeps the
    whole directory alive whether or not the session has stopped since. Only an override left untouched
    past the cutoff, on a session also unseen that long, is swept, and by then it is as stale as the
    frozen shared value beside it.

    Run once per new session - at the stop that first records it - not on every stop: the pass stats
    each session directory, so its cost is proportional to how many exist, and paying that once per
    session is the cheapest cadence that still reaps every session that goes stale. That count is what
    the sweep itself bounds; a busy machine that keeps resuming sessions within the cutoff carries all
    of them and pays for all of them each new session, which is the price of never reaping a live one.
    `keep` is the caller's own directory, brand new this stop; it is skipped by name rather than left
    to the cutoff, so a future reader need not reason about whether now-precedes-the-cutoff protects
    it. [LAW:no-ambient-temporal-coupling]

    [LAW:effects-at-boundaries][LAW:no-silent-failure] best-effort per entry and non-fatal overall,
    like the sweep's sibling `mark_seen` and `log`: housekeeping must not take the gate down, so a
    directory that will not remove is reported and the rest are still swept. The one refusal not
    reported is the entry already being gone: two new sessions can sweep at once, and the one that
    loses the race to remove a given entry finds it missing - a no-op, not a failure to announce.

    The mirror of `lines_in`'s read-side race is a write-side one, and it is accepted here, not guarded.
    A session whose record aged past the cutoff can resume and write into its own directory in the
    window between this pass judging it stale and removing it. The hook's own write - `mark_seen`'s
    `os.utime` - already catches the directory vanishing and carries on, so it is untouched. The
    `ceiling` command re-creates the directory with `mkdir(parents=True)` before it stages, healing the
    common case; only a delete landing inside its sub-millisecond stage-then-replace window makes it
    fail, and that is a loud, retryable command error - never a wrong or absent gate. The precondition
    (a session unseen for a month yet active enough to be writing, and a second new session sweeping at
    that instant) is vanishingly rare, and a loud retryable failure is the safe direction to err; a lock
    spanning every write to a session directory is not worth its carrying cost for it. [LAW:carrying-cost]"""
    cutoff = time.time() - STALE_SESSION_AGE_SECONDS
    keep = keep.resolve()
    try:
        entries = list(SESSION_CONFIGS.iterdir())
    except FileNotFoundError:
        return
    except OSError as failure:
        # A tree that is not readable at all - permissions, a dead mount - is nothing this pass can
        # sweep, but it is also not a reason to take the gate down. Report and leave, like every other
        # arm here. [LAW:no-silent-failure]
        print(f"memento config: cannot sweep {SESSION_CONFIGS}: {failure}", file=sys.stderr)
        return
    for entry in entries:
        try:
            if entry.resolve() == keep or _last_seen(entry) >= cutoff:
                continue
            # A directory carries its own partials; a stray partial at the tree root has none to
            # carry. [LAW:dataflow-not-control-flow] the staleness decision above is one rule for
            # both, and only the removal splits on what the filesystem needs to remove each shape.
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        except FileNotFoundError:
            # The entry, or a file inside it read mid-scan, is already gone: another concurrent sweep
            # removed it, or its own session is writing and replacing files under it. Either way there
            # is nothing to reap this pass and nothing wrong to report - the next new session's sweep
            # revisits whatever remains. A lost race is a no-op, not the failure the arm below reports.
            # [LAW:no-silent-failure]
            pass
        except OSError as failure:
            print(f"memento config: cannot sweep {entry}: {failure}", file=sys.stderr)


def folded(layers, beneath=lambda: DEFAULT_CEILING):
    """Every written layer's move applied to the ceiling beneath it, in order.

    [LAW:single-enforcer] a resolved ceiling is checked for sense here, where every fold passes,
    rather than at one of them. The shared fold is the one that gets written down, and a negative
    reaching the record is unrecoverable: `-50000` is written, read back as an *adjustment*, and
    resolves to 200,000 - a positive ceiling nobody set, in place of the loud exit. The format
    cannot express a negative absolute and is never asked to, because no layer may resolve to
    one.

    `beneath` is asked for rather than given, and the fold is assembled before it runs, so a stack
    of layers whose topmost states a ceiling outright never asks at all. A caller writing over the
    very file its base would come from is the reason: for `400_000` there is nothing it needs from
    that file, and an unreadable one must not stop it from replacing it."""
    ceiling = beneath
    for setting in layers:
        ceiling = functools.partial(parse_ceiling(setting), ceiling)
    resolved = ceiling()
    if resolved < 0:
        sys.exit(f"memento config: a ceiling of {resolved:,} tokens is set by "
                 f"{', then '.join(one.source for one in layers)}, and a count of tokens is "
                 f"never negative.")
    return resolved


def live_shared(anchor):
    """The user and project layers folded as they stand right now. This is the ceiling a session
    starting here would begin under, and the only reader that wants it mid-session is one asking
    what a change to those layers would mean for the next session rather than for this one."""
    return folded([one for one in (ceiling_in(USER_CONFIG), project_ceiling(anchor)) if one])


def staged(path, ceiling):
    """One config file's new content, written beside where it is going and not yet in place.

    [LAW:effects-at-boundaries] the one place a ceiling becomes bytes. Written whole and staged
    rather than into the destination, because a create-then-write leaves the file empty for the
    width of one flush: a session killed inside that window comes back to a file that exists and
    parses to nothing, which no later stop can complete and every later stop dies on - the gate
    off for that session, permanently, with nothing in the log to say so.

    Apart from `committed` so that a caller writing several files that have to state one number
    can stage all of them before any of them lands. The failures that happen - no permission, no
    space, a parent that cannot be made - happen here, where nothing is in place yet; `staging`
    coordinates more than one of these and owns what a half-finished pass leaves behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}")
    partial.write_text(f"{CEILING_KEY} = {render(ceiling)}\n")
    return partial


def committed(partial, path):
    """A staged file moved into place, and what the config there now says.

    After `os.replace` the destination holds the old content or the new, and never the third
    thing.

    Read back rather than handed back, so the writer and every later reader quote one file rather
    than two spellings of it, and so a value this wrote that `ceiling_in` would refuse fails here
    on the write rather than on the next stop that reads it. [LAW:one-source-of-truth] that
    read-back is also what leaves a writer nothing to guess: the only text that reaches disk is
    text the reader above accepts."""
    os.replace(partial, path)
    return ceiling_in(path)


@contextlib.contextmanager
def staging(paths, ceiling):
    """Every path staged for one ceiling, and nothing staged left behind.

    The pass that can fail is the staging one, so it finishes before the caller commits anything:
    files that have to state one number cannot end up stating two. What that leaves to account for
    is the partials themselves, and two different halts leave one - a staging pass that raises part
    way through, and a commit pass that stops with partials still waiting. Both are the same
    question asked of `unlink(missing_ok=True)`, because a partial `committed` has already consumed
    is simply not there, so one `finally` answers both and a failure litters nothing.
    [LAW:no-silent-failure] a stray `memento.conf.<pid>` beside a project's config is invisible to
    every reader here and to the person whose repo it is in."""
    partials = []
    try:
        for path in paths:
            partials.append((path, staged(path, ceiling)))
        yield partials
    finally:
        for _, partial in partials:
            partial.unlink(missing_ok=True)


def write_ceiling(path, ceiling):
    """One config file replaced by the ceiling it now sets, for a caller writing exactly one."""
    return committed(staged(path, ceiling), path)


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
    write is reached only for a path `ceiling_in` read nothing from. The one thing that does
    replace a standing record is `session_shared`, and only once a reset in place has landed - a
    lifecycle event, not the mid-run re-read this freeze exists to refuse.

    Returns the record and whether this call created it. The one caller maintaining the sessions tree
    needs to know if this was the session's first stop, and this function already knows - it just chose
    whether to write. Reporting it here is one existence question answered once, rather than the caller
    asking the filesystem the same thing a second time. [LAW:one-source-of-truth]"""
    existing = ceiling_in(path)
    if existing:
        return existing, False
    return write_ceiling(path, live_shared(anchor)), True


def mark_reset_pending(directory, tokens):
    """Record the context size at a credited close-out, so a later stop can tell whether the reset it
    scheduled actually landed (the context is now smaller) or never did (it is not).

    [LAW:effects-at-boundaries] Best-effort like `mark_seen`: a marker that fails to write costs the
    session one shared refresh until its next close-out, never the gate, so the failure is reported and
    not raised. [LAW:no-silent-failure] A torn write can only shorten the number, which lowers the
    threshold and so can only *miss* a re-derive, never force a wrong one - the safe direction - which
    is why this needs none of the record's staged-then-replaced care."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / RESET_PENDING).write_text(f"{tokens}\n")
    except OSError as failure:
        print(f"memento config: cannot mark reset pending in {directory}: {failure}", file=sys.stderr)


def reset_pending_tokens(marker):
    """The context size recorded when this session's close-out was credited, or None if no reset is
    pending.

    The marker is memento's own file holding one integer; a missing or unreadable one is treated as no
    pending reset, healing at the next close-out, because a stale marker must never take the gate down
    the way a raise from here would. [LAW:no-silent-failure]"""
    try:
        return int(marker.read_text())
    except (FileNotFoundError, ValueError):
        return None
    except OSError as failure:
        print(f"memento config: cannot read {marker}: {failure}", file=sys.stderr)
        return None


def session_shared(directory, anchor, tokens):
    """The shared ceiling this session holds now, re-derived once a credited close-out's reset lands.

    `shared_at_start` freezes the shared layers for a session's life, so a file a stranger edits cannot
    move a running session's ceiling mid-run. The tmux transport resets a session in place, keeping its
    id and so that record, which would freeze the *first* context's ceiling onto every context after it
    - the staleness this ticket is about. A close-out leaves `mark_reset_pending` holding the context
    size at that moment; a later stop whose context has fallen below it is running the reset's fresh,
    smaller context, so the record is re-derived from the shared layers as they now stand and the marker
    is spent. A context that has NOT fallen - a reset scheduled but never landed - keeps the record, so a
    session still running its original large context is never re-frozen from files that moved under it,
    which is the very thing the record exists to prevent. [LAW:no-ambient-temporal-coupling]

    The test is conservative on purpose. A fresh context grown back past the close-out size before its
    first stop reads as 'not landed' and keeps the frozen value, healing at a later close-out rather
    than risking a re-freeze of a live context: a missed refresh is the mild original bug, a wrong one
    is the dangerous one. A re-derive is not the session's first stop - the record and the sessions-tree
    sweep both stand from the real first stop - so it reports `False`."""
    record = directory / SHARED_AT_START
    marker = directory / RESET_PENDING
    frozen_at = reset_pending_tokens(marker)
    if frozen_at is not None and tokens < frozen_at:
        # Re-derive first, spend the marker second. `write_ceiling` and `live_shared` can fail - a
        # shared file broken at this moment exits here, exactly as it does for any fresh start into a
        # broken config - and a marker unlinked before that write would lose the landing to the
        # failure, leaving the stale value with nothing to retry it. Spending it only after the record
        # is rewritten makes the re-derive idempotent across a retry: a stop that fails leaves the
        # marker standing, so the next one tries again. The unlink is bookkeeping, so its own failure
        # is reported and not raised - a marker that will not clear costs repeated harmless re-derives,
        # never the gate. [LAW:no-silent-failure]
        written = write_ceiling(record, live_shared(anchor))
        try:
            marker.unlink(missing_ok=True)
        except OSError as failure:
            print(f"memento config: cannot clear {marker}: {failure}", file=sys.stderr)
        return written, False
    return shared_at_start(record, anchor)


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
