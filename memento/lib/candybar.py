#!/usr/bin/env python3
"""Installs memento's cc-candybar plugin and writes each session's ceiling status for it.

The plugin reads `session/<session id>.json` from its folder. memento writes that file with the
ceiling in force, computed from the same layers the Stop hook gates on. The installed definition is
the shipped `plugin.json5` under a version stamp line, and the newest installed copy is kept.
cc-candybar creates the `session` folder and sweeps its stale files, so nothing is written until it exists.
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
# The command the plugin's controls run is stored in each session's status, not in a shared file,
# because each plugin version and profile has its own install path.
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
    """Returns the shipped definition under a first line stamped with this memento's version.

    Read at call time rather than import, so importing this module never fails."""
    version = json.loads(MANIFEST.read_text())["version"]
    return f"// memento {version}\n".encode() + SHIPPED_DEFINITION.read_bytes()


def version_in(data):
    """Returns the version in a definition's stamp line as a tuple for comparison, or () if there is none."""
    stamp = STAMP.match(data)
    return tuple(int(part) for part in stamp.group(1).split(b".")) if stamp else ()


def bytes_at(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def placed(path, data, mode):
    """Writes a file whole by staging a dot-named partial beside it and renaming it into place.

    The rename matters because the daemon loads files as they appear in its folder. cc-candybar
    ignores dot-names, so a partial left by a failed write is never loaded or swept."""
    partial = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        partial.write_bytes(data)
        partial.chmod(mode)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def created(data):
    """Creates the plugin folder with its definition, staged and renamed so it appears complete.

    Returns False if another run created the folder first; the caller then compares the existing one."""
    PLUGINS.mkdir(parents=True, exist_ok=True)
    # A name no other run holds, so a stage a killed run left behind never blocks this one.
    stage = Path(tempfile.mkdtemp(prefix=".memento-", dir=PLUGINS))
    try:
        # mkdtemp creates the stage with mode 0700, which the rename would carry to the folder.
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
    """Brings the plugin folder to this memento's definition and returns what changed.

    Returns `installed`, `updated`, or `current`; `newer` means a newer memento's definition is in
    place and is left alone. A current folder is not written, because each write makes cc-candybar
    reload the plugin."""
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
    """Returns the session's status as the plugin reads it.

    The status holds the ceiling in force and the session's own layer, or a `refusal` message if a
    layer cannot be read. `home` and `command` are included in every status, refusals too, so the
    bar's controls keep working."""
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
    """Returns the session's status file in the plugin's storage, named after its session directory."""
    return STORAGE / f"{directory.name}.json"


def write_status(directory, anchor):
    """Writes the session's status by rename. Returns `written`, or `no-storage` if cc-candybar has
    not yet created the plugin's session folder."""
    path = status_file(directory)
    try:
        placed(path, json.dumps(status(directory, anchor)).encode(), 0o644)
    except FileNotFoundError:
        if STORAGE.is_dir():
            raise
        return "no-storage"
    return "written"


def publish(directory, anchor):
    """Installs the plugin and writes the session's status, returning both results as facts.

    Raises OSError if the filesystem refuses a write."""
    return {"plugin": install(), "status": write_status(directory, anchor)}


def published(hook):
    """Runs `publish` for a hook payload and returns the facts for its log line.

    A failure is printed to stderr and returned as a fact instead of raised, so a bar update cannot
    stop the hook's own work."""
    try:
        return publish(session_directory(hook["session_id"]), anchored(hook["cwd"]))
    except (Exception, SystemExit) as failure:
        print(f"memento: cc-candybar plugin not updated: {reason_of(failure)}", file=sys.stderr)
        return {"candybar": f"failed: {reason_of(failure)}"}
