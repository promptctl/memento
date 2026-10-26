#!/usr/bin/env python3
"""memento's cc-candybar plugin: the folder memento installs it in, and each session's status there.

cc-candybar shows another program on its bar through a plugin that program ships - a folder in
cc-candybar's user plugin root holding `plugin.json5` - and names no program of its own accord.
memento's shows the ceiling in force for a session and moves it. The plugin reads one file per
session, `session/<session id>.json` in its folder, and memento writes it.

[LAW:one-source-of-truth] what that file says is `in_force`, the number the Stop hook gates on,
asked of the same layers the same way; the bar never reads memento's layers itself, and a move it
makes is memento's own `ceiling` command. The definition is memento's shipped copy, installed
byte for byte, so the reader on disk is always the one this writer shipped with.

cc-candybar owns the `session` folder: it creates it when a daemon loads the plugin and sweeps the
files no session has written in 30 days. memento only writes into it, so on a machine where no
cc-candybar has loaded the plugin no file is written that nothing would ever sweep.
"""

import json
import math
import os
import shlex
import shutil
import sys
from pathlib import Path

from ceiling_config import (CONFIG_HOME, CONFIG_NAME, anchored, ceiling_in, in_force,
                            session_directory)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_DEFINITION = PLUGIN_ROOT / "cc-candybar" / "plugin.json5"
COMMANDS = PLUGIN_ROOT / "bin" / "memento"
# The user's plugin root, as cc-candybar resolves it: where an app drops a plugin folder.
PLUGINS = (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
           / "cc-candybar" / "plugins")
FOLDER = PLUGINS / "memento"
STORAGE = FOLDER / "session"
DEFINITION_NAME = "plugin.json5"
LAUNCHER_NAME = "memento"


def launcher():
    """The launcher the plugin's actions run: memento's commands, at this install of memento.

    A `run` action starts in the plugin's folder with no shell and no PATH lookup, so the folder
    holds the program itself. Claude Code keeps each version of a plugin at its own path, so the
    path is written at install, and the hooks of an upgraded memento rewrite it."""
    return (f"#!/bin/sh\n"
            f"# memento's commands, as installed at {PLUGIN_ROOT}; written by its hooks.\n"
            f"exec {shlex.quote(str(COMMANDS))} \"$@\"\n").encode()


def contents():
    """The folder as this memento ships it: each file's bytes and mode, the launcher first, so a
    definition naming it never lands before it."""
    return {LAUNCHER_NAME: (launcher(), 0o755),
            DEFINITION_NAME: (SHIPPED_DEFINITION.read_bytes(), 0o644)}


def bytes_at(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def placed(path, data, mode):
    """One file written whole: staged under a dot-name, which cc-candybar's load never reads, then
    renamed into place, because the daemon watches this folder and loads what appears in it."""
    partial = path.with_name(f".{path.name}.{os.getpid()}")
    partial.write_bytes(data)
    partial.chmod(mode)
    os.replace(partial, path)


def created(wanted):
    """A new plugin folder, staged whole in the root and renamed into place, so the daemon never
    sees a plugin folder with no definition in it, which it reports as a broken plugin. False when
    another hook's rename won, and the folder it placed is there to compare. The stage is a
    dot-name, which cc-candybar's load never reads. [LAW:no-ambient-temporal-coupling]"""
    PLUGINS.mkdir(parents=True, exist_ok=True)
    stage = PLUGINS / f".memento-{os.getpid()}"
    stage.mkdir()
    try:
        for name, (data, mode) in wanted.items():
            placed(stage / name, data, mode)
        stage.rename(FOLDER)
        return True
    except OSError:
        if not FOLDER.is_dir():
            raise
        return False
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def install():
    """The plugin folder brought to what this memento ships, and what that took: `installed`,
    `updated` or `current`. A folder already there has only its differing files replaced, so a
    current one sees no write at all: cc-candybar watches the root and this runs at every stop."""
    wanted = contents()
    if not FOLDER.is_dir() and created(wanted):
        return "installed"
    stale = [name for name, (data, _) in wanted.items() if bytes_at(FOLDER / name) != data]
    for name in stale:
        placed(FOLDER / name, *wanted[name])
    return "updated" if stale else "current"


def status(directory, anchor):
    """The session's status as the plugin reads it: the ceiling in force and the session's own
    layer, or the refusal that stops memento's gate for this session.

    [LAW:no-silent-failure] a layer the reader refuses is shown on the bar as that refusal, in the
    error colour, rather than as a ceiling the gate is no longer enforcing."""
    try:
        ceiling = in_force(directory, anchor)
        layer = ceiling_in(directory / CONFIG_NAME)
    except SystemExit as refusal:
        return {"home": str(CONFIG_HOME), "refusal": str(refusal.code)}
    except OSError as refusal:
        return {"home": str(CONFIG_HOME), "refusal": f"memento config: {refusal}"}
    return {"home": str(CONFIG_HOME),
            "ceiling": "off" if ceiling == math.inf else ceiling,
            "session": layer.text if layer else None}


def status_file(directory):
    """The session's file in the plugin's storage, named by the session directory
    `session_directory` already proved names one session and nothing else.
    [LAW:single-enforcer]"""
    return STORAGE / f"{directory.name}.json"


def write_status(directory, anchor):
    """The session's status written whole, by rename, and what became of it: `written`, or
    `no-storage` where no cc-candybar has loaded the plugin and created its folder."""
    path = status_file(directory)
    try:
        placed(path, json.dumps(status(directory, anchor)).encode(), 0o644)
    except FileNotFoundError:
        if STORAGE.is_dir():
            raise
        return "no-storage"
    return "written"


def publish(directory, anchor):
    """The plugin installed and the session's status written: what every hook and every `ceiling`
    command does, as the facts its caller reports. A filesystem that says no raises OSError."""
    return {"plugin": install(), "status": write_status(directory, anchor)}


def published(hook):
    """`publish` for a hook's payload, as the facts its log line carries.

    A hook's job is its own - the gate, the dropped override - and a bar that could not be updated
    must not take it down, so a failure here is one more fact on the hook's line, and on stderr. A
    payload with no session to publish for - the failure a hook is already reporting - is that same
    fact. [LAW:no-silent-failure]"""
    try:
        return publish(session_directory(hook["session_id"]), anchored(hook["cwd"]))
    except (Exception, SystemExit) as failure:
        reason = (failure.code if isinstance(failure, SystemExit)
                  else f"{type(failure).__name__}: {failure}")
        print(f"memento: cc-candybar plugin not updated: {reason}", file=sys.stderr)
        return {"candybar": f"failed: {reason}"}
