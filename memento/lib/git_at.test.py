#!/usr/bin/env python3
"""Tests for git_at: git asked about a directory answers for the directory's own repository whatever
repository the inherited environment names, and keeps the settings `git -c` carries.

Run with: python3 git_at.test.py
"""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile

SCRATCH = tempfile.mkdtemp(prefix="git-at-test-")
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
# The suite's environment is PATH of the one it runs in and SCRATCH as HOME, nothing else, so the
# repositories it sets up are its own though it runs from a git hook, and each case adds only what
# it inherits.
path = os.environ["PATH"]
os.environ.clear()
os.environ.update(PATH=path, HOME=SCRATCH)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from git_at import git_at  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def repository(name):
    path = os.path.join(SCRATCH, name)
    subprocess.run(["git", "init", "-q", path], check=True, capture_output=True)
    return path


def inheriting(variables, *arguments):
    """Runs git_at about `here` with `variables` in this process's environment, as a child of a git
    hook would have them."""
    os.environ.update(variables)
    try:
        return git_at(here, *arguments)
    finally:
        for name in variables:
            del os.environ[name]


here, other = repository("here"), repository("other")
WHERE = ("rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir",
         "--git-path", "index", "--git-path", "objects")
# What git says of `here` with no GIT_ variable at all: the answer owed whatever is inherited.
own = subprocess.run(["git", *WHERE], cwd=here, capture_output=True, text=True, check=True).stdout

# A hook in a linked worktree runs with more than GIT_DIR set: each of these names the other repository.
asked = inheriting({"GIT_DIR": os.path.join(other, ".git"), "GIT_WORK_TREE": other,
                    "GIT_COMMON_DIR": os.path.join(other, ".git"),
                    "GIT_INDEX_FILE": os.path.join(other, ".git", "index"),
                    "GIT_OBJECT_DIRECTORY": os.path.join(other, ".git", "objects")}, *WHERE)
check("the directory's repository answers whatever repository the environment names",
      asked.returncode == 0 and asked.stdout == own, f"{asked.stdout!r} {asked.stderr!r} != {own!r}")

counted = inheriting({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "memento.counted",
                      "GIT_CONFIG_VALUE_0": "kept"}, "config", "memento.counted")
check("a setting GIT_CONFIG_COUNT carries reaches the directory's repository",
      counted.stdout == "kept\n", f"{counted.stdout!r} {counted.stderr!r}")
passed = inheriting({"GIT_CONFIG_PARAMETERS": "'memento.passed'='kept'"}, "config", "memento.passed")
check("and so does one `git -c` carries", passed.stdout == "kept\n",
      f"{passed.stdout!r} {passed.stderr!r}")

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
