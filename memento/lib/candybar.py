#!/usr/bin/env python3
"""memento's cc-candybar plugin: the folder memento installs it in, and each session's status there.

cc-candybar shows another program on its bar through a plugin that program ships - a folder in
cc-candybar's user plugin root holding `plugin.json5` - and names no program of its own accord.
memento's shows the ceiling in force for a session and moves it. The plugin reads one file per
session, `session/<session id>.json` in its folder, and memento writes it.

[LAW:one-source-of-truth] what that file says is `in_force`, the number the Stop hook gates on,
asked of the same layers the same way; the bar never reads memento's layers itself, and a move it
makes is memento's own `ceiling` command. The definition is memento's shipped copy, installed
byte for byte under a line naming the memento that installed it, so the reader on disk is one a
memento shipped, and the newest installed.

cc-candybar owns the `session` folder: it creates it when a daemon loads the plugin and sweeps the
files no session has written in 30 days. memento only writes into it, so on a machine where no
cc-candybar has loaded the plugin no file is written that nothing would ever sweep.
"""

import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from ceiling_config import (CONFIG_HOME, CONFIG_NAME, anchored, ceiling_in, in_force,
                            session_directory)
from ceiling_log import reason_of

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_DEFINITION = PLUGIN_ROOT / "cc-candybar" / "plugin.json5"
MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
# What the plugin's actions run, carried in each session's status rather than in a file beside the
# definition: Claude Code keeps each version of a plugin, and each profile's copy, at its own path,
# and a session's controls must run the memento whose hooks that session runs. One path shared by
# the folder would be whichever install's hook ran last, rewritten back and forth by two profiles.
CEILING_COMMAND = PLUGIN_ROOT / "skills" / "ceiling" / "bin" / "ceiling"
# The user's plugin root, as cc-candybar resolves it: where an app drops a plugin folder.
PLUGINS = (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
           / "cc-candybar" / "plugins")
FOLDER = PLUGINS / "memento"
STORAGE = FOLDER / "session"
DEFINITION_NAME = "plugin.json5"
# The first line of an installed definition, naming the memento that installed it.
STAMP = re.compile(rb"// memento ([0-9]+(?:\.[0-9]+)*)\b[^\n]*\n")


def definition():
    """The definition as this memento installs it: the shipped file under a first line naming this
    memento's version, which is how an install tells a newer memento's copy from an older one.
    Read here rather than at import, so a hook whose plugin root cannot say still runs its gate."""
    version = json.loads(MANIFEST.read_text())["version"]
    return f"// memento {version}\n".encode() + SHIPPED_DEFINITION.read_bytes()


def version_in(data):
    """The memento version a definition's first line names, as numbers to compare, or () for one
    that names none - a hand edit, or no stamp at all - which any memento replaces."""
    stamp = STAMP.match(data)
    return tuple(int(part) for part in stamp.group(1).split(b".")) if stamp else ()


def bytes_at(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def placed(path, data, mode):
    """One file written whole: staged under a dot-name, which cc-candybar's load never reads, then
    renamed into place, because the daemon watches this folder and loads what appears in it.

    [LAW:no-silent-failure] a write that fails leaves no partial: a dot-name is invisible to
    cc-candybar's load and to its sweep alike, so one left behind would stay forever."""
    partial = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        partial.write_bytes(data)
        partial.chmod(mode)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def created(data):
    """A new plugin folder, staged whole in the root and renamed into place, so the daemon never
    sees a plugin folder with no definition in it, which it reports as a broken plugin. False when
    another hook's rename won, and the folder it placed is there to compare. The stage is a
    dot-name, which cc-candybar's load never reads. [LAW:no-ambient-temporal-coupling]"""
    PLUGINS.mkdir(parents=True, exist_ok=True)
    # A name no other run holds, so a stage a killed run left behind never blocks this one.
    stage = Path(tempfile.mkdtemp(prefix=".memento-", dir=PLUGINS))
    try:
        # mkdtemp's 0700 would become the plugin folder's mode with the rename.
        stage.chmod(0o755)
        placed(stage / DEFINITION_NAME, data, 0o644)
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
    `updated`, `current`, or `newer` where a newer memento's definition is in place, which this one
    leaves standing. A current folder sees no write at all: cc-candybar watches the root and this
    runs at every stop.

    [LAW:one-source-of-truth] the folder holds the newest definition any installed memento ships.
    Two profiles, or two versions of the plugin, each running its own memento, would otherwise
    rewrite it at every stop of either, and the daemon reload the plugin each time. So a newer
    memento's definition reads the status every older one writes."""
    wanted = definition()
    if not FOLDER.is_dir() and created(wanted):
        return "installed"
    there = bytes_at(FOLDER / DEFINITION_NAME)
    if there == wanted:
        return "current"
    if there is not None and version_in(there) > version_in(wanted):
        return "newer"
    placed(FOLDER / DEFINITION_NAME, wanted, 0o644)
    return "updated"


def status(directory, anchor):
    """The session's status as the plugin reads it: the ceiling in force and the session's own
    layer, or the refusal that stops memento's gate for this session.

    [LAW:no-silent-failure] a layer the reader refuses is shown on the bar as that refusal, in the
    error colour, rather than as a ceiling the gate is no longer enforcing. `home` and `command`
    are in every status, a refusal's too, because the control beside a refusal is a move."""
    where = {"home": str(CONFIG_HOME), "command": str(CEILING_COMMAND)}
    try:
        ceiling = in_force(directory, anchor)
        layer = ceiling_in(directory / CONFIG_NAME)
    except SystemExit as refusal:
        return {**where, "refusal": str(refusal.code)}
    except OSError as refusal:
        return {**where, "refusal": f"memento config: {refusal}"}
    return {**where,
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
        print(f"memento: cc-candybar plugin not updated: {reason_of(failure)}", file=sys.stderr)
        return {"candybar": f"failed: {reason_of(failure)}"}
