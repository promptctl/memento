#!/usr/bin/env python3
"""Tests for memento's cc-candybar plugin: the folder memento installs and the status it writes.

Every case runs memento's real programs as subprocesses - the hooks with a payload on stdin, and
the `ceiling` command a session's status names, started as cc-candybar's run starts it - under a
scratch config home and a scratch XDG_DATA_HOME, then reads what landed on disk. [LAW:behavior-not-structure]
cc-candybar reads only those files, so they are the contract.

Run: python3 candybar.test.py
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRATCH = tempfile.mkdtemp(prefix="candybar-test-")
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)

LIB = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(LIB)
STOP_HOOK = os.path.join(PLUGIN, "hooks", "scripts", "context-ceiling.py")
CLEAR_HOOK = os.path.join(PLUGIN, "hooks", "scripts", "clear-session-ceiling.py")
STATUS_HOOK = os.path.join(PLUGIN, "hooks", "scripts", "session-status.py")
CEILING = os.path.join(PLUGIN, "skills", "ceiling", "bin", "ceiling")
SHIPPED = os.path.join(PLUGIN, "cc-candybar", "plugin.json5")
sys.path.insert(0, LIB)
from ceiling_config import CONFIG_NAME, DEFAULT_CEILING  # noqa: E402

SESSION = "0b6c3a4e-5d1f-4a7e-9c2b-8e1f0a3d5c71"
failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


class World:
    """A config home, a project, a plugin root and a log, all scratch; `storage` says whether a
    cc-candybar daemon has loaded the plugin and created its session folder."""

    def __init__(self, storage=True, user_conf=None):
        self.root = tempfile.mkdtemp(dir=SCRATCH)
        self.home = os.path.join(self.root, "home")
        self.project = os.path.join(self.root, "project")
        self.data = os.path.join(self.root, "data")
        self.log = os.path.join(self.root, "ceiling.log")
        self.folder = os.path.join(self.data, "cc-candybar", "plugins", "memento")
        self.transcript = os.path.join(self.root, "transcript.jsonl")
        for d in (self.home, self.project):
            os.makedirs(d)
        open(self.transcript, "w").close()
        if storage:
            os.makedirs(os.path.join(self.folder, "session"))
        if user_conf is not None:
            with open(os.path.join(self.home, CONFIG_NAME), "w") as handle:
                handle.write(user_conf)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "XDG_CONFIG_HOME", "CLAUDE_CODE_SESSION_ID")}
        env.update({"MEMENTO_CONFIG_HOME": self.home, "XDG_DATA_HOME": self.data,
                    "MEMENTO_CEILING_LOG": self.log, **extra})
        return env

    def hook(self, script, event, cwd=None, env=None, **payload):
        body = {"hook_event_name": event, "session_id": SESSION, "cwd": self.project,
                "transcript_path": self.transcript, **payload}
        return subprocess.run([sys.executable, script], input=json.dumps(body), text=True,
                              capture_output=True, cwd=cwd, env=env or self.env())

    def where(self):
        """What every status carries: the config home and the session's `ceiling` command."""
        return {"home": self.home, "command": CEILING}

    def ceiling(self, *argv, program=None, cwd=None):
        """`ceiling` as cc-candybar's run starts it: the session's id and project in the
        environment, in whatever directory the caller names."""
        return subprocess.run([*(program or [sys.executable, CEILING]), *argv], text=True,
                              capture_output=True, cwd=cwd or self.project,
                              env=self.env(CLAUDE_CODE_SESSION_ID=SESSION,
                                           CLAUDE_PROJECT_DIR=self.project))

    def status(self):
        path = os.path.join(self.folder, "session", f"{SESSION}.json")
        return json.load(open(path)) if os.path.exists(path) else None

    def last_log(self):
        return open(self.log).read().splitlines()[-1]


# --- the install ---

w = World(storage=False)
done = w.hook(STATUS_HOOK, "SessionStart", source="startup")
check("a starting session's hook succeeds where no cc-candybar has loaded the plugin",
      done.returncode == 0, done.stderr)
check("it installs the plugin's definition byte for byte",
      open(os.path.join(w.folder, "plugin.json5"), "rb").read() == open(SHIPPED, "rb").read())
check("and nothing beside it",
      os.listdir(w.folder) == ["plugin.json5"], os.listdir(w.folder))
check("and leaves no stage behind in the plugin root",
      os.listdir(os.path.dirname(w.folder)) == ["memento"], os.listdir(os.path.dirname(w.folder)))
check("no session folder: no status is written, and nothing that nothing would sweep",
      not os.path.exists(os.path.join(w.folder, "session")))
check("the log line says what the install did and what became of the status",
      "plugin=installed status=no-storage -> published" in w.last_log(), w.last_log())

stamp = os.stat(os.path.join(w.folder, "plugin.json5")).st_mtime_ns
w.hook(STATUS_HOOK, "SessionStart", source="resume")
check("an install that is current writes nothing, so the watched root sees no change",
      os.stat(os.path.join(w.folder, "plugin.json5")).st_mtime_ns == stamp
      and "plugin=current" in w.last_log(), w.last_log())

with open(os.path.join(w.folder, "plugin.json5"), "w") as handle:
    handle.write("{ an older memento's definition }")
w.hook(STATUS_HOOK, "SessionStart", source="startup")
check("a definition that differs from this memento's is replaced",
      open(os.path.join(w.folder, "plugin.json5"), "rb").read() == open(SHIPPED, "rb").read()
      and "plugin=updated" in w.last_log(), w.last_log())

# --- the status ---

w = World()
w.hook(STATUS_HOOK, "SessionStart", source="startup")
check("a starting session's status is the ceiling in force, with no session layer",
      w.status() == {**w.where(), "ceiling": DEFAULT_CEILING, "session": None}, w.status())

done = w.ceiling("set", "session", "+100_000")
check("a move writes the session's status after it", done.returncode == 0
      and w.status() == {**w.where(), "ceiling": DEFAULT_CEILING + 100_000,
                         "session": str(DEFAULT_CEILING + 100_000)}, (done.stderr, w.status()))
check("and says so in its output", "cc-candybar  plugin=current status=written" in done.stdout,
      done.stdout)

w.ceiling("set", "session", "off")
check("a lifted ceiling reads off", w.status()["ceiling"] == "off"
      and w.status()["session"] == "off", w.status())

done = w.hook(STOP_HOOK, "Stop", stop_hook_active=False)
check("every stop writes the status the gate resolved", done.returncode == 0
      and w.status()["ceiling"] == "off" and "status=written" in w.last_log(),
      (done.stderr, w.last_log()))

done = w.hook(CLEAR_HOOK, "SessionStart", source="clear")
check("/clear drops the session's layer and the status shows the ceiling beneath",
      done.returncode == 0
      and w.status() == {**w.where(), "ceiling": DEFAULT_CEILING, "session": None},
      (done.stderr, w.status()))

# --- a refusal ---

w = World(user_conf="ceiling = lots\n")
done = w.hook(STOP_HOOK, "Stop", stop_hook_active=False)
status = w.status() or {}
check("a stop over a layer the reader refuses still fails loudly", done.returncode != 0,
      done.stderr)
check("and the status carries memento's refusal, for the bar to show",
      set(status) == {"home", "command", "refusal"} and "lots" in status.get("refusal", ""),
      status)
os.remove(os.path.join(w.folder, "session", f"{SESSION}.json"))
done = w.ceiling("show")
check("so does `ceiling`, which exits 1 over it as before",
      done.returncode == 1 and "lots" in (w.status() or {}).get("refusal", ""),
      (done.returncode, w.status()))

w = World(user_conf="ceiling = lots\n")
done = w.ceiling("set", "session", "+100_000")
check("a move refused over an unusable layer still writes the refusal for the bar",
      done.returncode == 1 and "lots" in (w.status() or {}).get("refusal", ""),
      (done.returncode, done.stderr, w.status()))

# --- the session's command, as cc-candybar's run starts it ---

w = World()
w.hook(STATUS_HOOK, "SessionStart", source="startup")
command = w.status()["command"]
check("the status names the session's `ceiling` command by absolute path",
      os.path.isabs(command) and os.access(command, os.X_OK), command)
done = w.ceiling("set", "session", "-100_000", program=[command], cwd=w.folder)
check("which runs from the plugin's folder, as the plugin's actions do",
      done.returncode == 0
      and w.status() == {**w.where(), "ceiling": DEFAULT_CEILING - 100_000,
                         "session": str(DEFAULT_CEILING - 100_000)}, (done.stderr, w.status()))
check("and every action runs the command the status names",
      open(SHIPPED).read().count('run: ["{{ .plugins.memento.status.command }}"') == 4)

# A config home named relative to the session's directory names another directory where
# cc-candybar runs the command, so the status carries it absolute.
w = World()
env = w.env(MEMENTO_CONFIG_HOME=os.path.relpath(w.home, w.project))
w.hook(STATUS_HOOK, "SessionStart", cwd=w.project, env=env, source="startup")
home = (w.status() or {}).get("home", "")
check("a relative config home is written absolute",
      os.path.isabs(home) and os.path.realpath(home) == os.path.realpath(w.home), w.status())

# --- a status that cannot be written ---

w = World()
w.hook(STATUS_HOOK, "SessionStart", source="startup")
blocked = os.path.join(w.folder, "session", f"{SESSION}.json")
os.remove(blocked)
os.mkdir(blocked)
done = w.ceiling("set", "session", "+100_000")
check("a status that cannot be written exits 3, and the move it follows stands",
      done.returncode == 3 and "cc-candybar status not written" in done.stderr
      and f"this session        {DEFAULT_CEILING + 100_000:,}" in done.stdout,
      (done.returncode, done.stderr, done.stdout))
check("and leaves no partial behind",
      os.listdir(os.path.join(w.folder, "session")) == [f"{SESSION}.json"],
      os.listdir(os.path.join(w.folder, "session")))

if failures:
    sys.exit(f"{len(failures)} failed: {', '.join(failures)}")
print("all passed")
