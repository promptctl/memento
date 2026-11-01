#!/usr/bin/env python3
"""Reads and writes the config layers that set the context ceiling, in one file format.

The Stop hook reads these layers and the `ceiling` command writes them, and both use this module so
they agree on the grammar. `staged` writes only text that `ceiling_in` accepts, and `committed`
reads back what was written. A file that is not in this format exits with its path and line named.
"""

import collections
import contextlib
import functools
import math
import os
import re
import sys
from pathlib import Path

DEFAULT_CEILING = 350_000
# Tokens a session may use past its ceiling to finish its current unit of work. The close-out is due
# at the next unit boundary once the ceiling is passed, and unconditionally at ceiling plus GRACE.
GRACE = 100_000
# The same filename at every layer.
CONFIG_NAME = "memento.conf"
# A repo carries its own config as a dot-directory, because a checkout has no XDG directory.
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
# Absolute, so the path resolves the same from programs started elsewhere, such as cc-candybar
# running `ceiling` from its plugin folder.
CONFIG_HOME = Path(os.environ.get("MEMENTO_CONFIG_HOME") or XDG_CONFIG / "promptctl").absolute()
USER_CONFIG = CONFIG_HOME / CONFIG_NAME
# Per-session state is only the override `ceiling set session` writes, at sessions/<id>/CONFIG_NAME.
# The ceiling in force is computed from the layers on each read, so nothing else is stored per session.
SESSION_CONFIGS = CONFIG_HOME / "sessions"
PROJECT_CONFIG_DIR = ".promptctl"
CEILING_KEY = "ceiling"
DISABLING_WORD = "off"
PROJECT_VARIABLE = "CLAUDE_PROJECT_DIR"
# [0-9] rather than \d, which matches digits that int() rejects.
CEILING_RE = re.compile(r"(?P<sign>[+-]?)(?P<digits>[0-9]+(?:_[0-9]+)*)\Z")
# A ceiling as one layer wrote it, carrying the file and line a person goes to change it.
Written = collections.namedtuple("Written", "source text")


def lines_in(path):
    """Returns the lines of one config file, or [] if no file exists at the path.

    Bytes that are not text exit with a message naming the file. A file removed between the existence
    check and the read also returns [], since any shared layer can be deleted at any time. Other OS
    errors, such as permission denied, are raised for the caller to handle."""
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
    """Returns the ceiling one config file sets, or None if it sets none.

    A line that is not `key = value`, or a key other than `ceiling`, exits, since an ignored line
    would look like a ceiling that was set."""
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
    """Returns the directory to look up the project layer from: CLAUDE_PROJECT_DIR if set, else `fallback`.

    Hooks and the `ceiling` command pass different fallbacks, and this is where they resolve the project."""
    return os.environ.get(PROJECT_VARIABLE) or fallback


def project_file(anchor):
    """Returns the project config file at or above a directory, or None.

    The walk stops at the first match, so a file at a repo root applies to its subdirectories and
    nested worktrees. The user's own config is skipped even where the walk reaches it, so one file is
    never applied as two layers."""
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
    """Returns the move one written ceiling makes, as a function of the ceiling beneath it.

    A count or `off` sets the ceiling outright and never calls the layer beneath, so that layer is
    passed as a thunk and read only when an adjustment needs it."""
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
    """Returns the config text for a resolved ceiling. `ceiling_in` reads back anything this renders."""
    return DISABLING_WORD if ceiling == math.inf else str(ceiling)


def session_directory(session_id):
    """Returns the directory holding one session's files.

    Exits if the id does not resolve to a single directory directly under SESSION_CONFIGS, such as an
    absolute path or one containing `..`. Containment is checked on the resolved path."""
    directory = (SESSION_CONFIGS / str(session_id)).resolve()
    if directory.parent != SESSION_CONFIGS.resolve():
        sys.exit(f"memento config: session id {session_id!r} names {directory}, which is not "
                 f"a session directory under {SESSION_CONFIGS}. Memento cannot tell which "
                 f"session's settings it was meant to read.")
    return directory


def folded(layers, beneath=lambda: DEFAULT_CEILING):
    """Applies each layer's move to the ceiling beneath it, in order, and returns the result.

    Exits if the result is negative, since a negative written to a file would read back as an
    adjustment. `beneath` is called only if a layer needs it, so a layer that sets a ceiling outright
    never reads the layer below."""
    ceiling = beneath
    for setting in layers:
        ceiling = functools.partial(parse_ceiling(setting), ceiling)
    resolved = ceiling()
    if resolved < 0:
        sys.exit(f"memento config: a ceiling of {resolved:,} tokens is set by "
                 f"{', then '.join(one.source for one in layers)}, and a count of tokens is "
                 f"never negative.")
    return resolved


def shared_layers(anchor):
    """The user and project layers as they stand right now, in the order they fold."""
    return [one for one in (ceiling_in(USER_CONFIG), project_ceiling(anchor)) if one]


def live_shared(anchor):
    """Returns the user and project layers folded: the ceiling a session without an override runs under."""
    return folded(shared_layers(anchor))


def in_force(directory, anchor):
    """Returns the ceiling in force for one session: the shared layers folded with the session's layer.

    The files are read on every call, so an edit to any layer applies at the next call. The order
    between layers is decided only here, so the Stop hook and the `ceiling` command agree on it."""
    return folded([*shared_layers(anchor), *filter(None, [ceiling_in(directory / CONFIG_NAME)])])


def staged(path, ceiling):
    """Writes one config file's new content beside its destination and returns the staged path.

    Staging avoids a crash leaving an empty config file, which would fail every later read. Errors
    such as a missing permission surface here, before anything is replaced. `staging` removes what a
    halted pass leaves behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}")
    partial.write_text(f"{CEILING_KEY} = {render(ceiling)}\n")
    return partial


def committed(partial, path):
    """Moves a staged file into place and returns the ceiling the config now holds.

    `os.replace` is atomic, so the destination holds either the old or the new content. The result is
    read back with `ceiling_in`, so text it would refuse fails here, at write time."""
    os.replace(partial, path)
    return ceiling_in(path)


@contextlib.contextmanager
def staging(paths, ceiling):
    """Stages a file for each path and removes any leftover partials when the block exits.

    All staging finishes before the caller commits anything, so a failure there leaves the real files
    untouched. The `finally` also covers a commit that stops partway."""
    partials = []
    try:
        for path in paths:
            partials.append((path, staged(path, ceiling)))
        yield partials
    finally:
        for _, partial in partials:
            partial.unlink(missing_ok=True)


