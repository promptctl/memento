#!/usr/bin/env python3
"""Tests for hermetic: a suite that calls `home`, started with variables a git hook and a Claude Code
session hand it naming real places, runs with PATH and one scratch directory as HOME and temp directory - its
own and its children's - and leaves nothing behind it when it exits.

Run with: python3 hermetic.test.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hermetic  # noqa: E402

hermetic.home()

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


PREAMBLE = """import json, os, subprocess, sys, tempfile
sys.path.insert(0, sys.argv[1])
import hermetic
scratch = hermetic.home()
"""


def suite(body):
    """Runs a suite that calls home and then `body`, under variables naming places in a new trap
    directory, and returns its run and those places."""
    trap = tempfile.mkdtemp()
    inherited = {name: os.path.join(trap, name.lower()) for name in ("HOME", "TMPDIR", "GIT_DIR")}
    for path in inherited.values():
        os.makedirs(path)
    probe = os.path.join(trap, "probe.test.py")
    with open(probe, "w") as f:
        f.write(PREAMBLE + body)
    done = subprocess.run([sys.executable, probe, HERE], capture_output=True, text=True,
                          env={**inherited, "PATH": os.environ["PATH"], "GIT_WORK_TREE": trap,
                               "CLAUDE_CODE_SESSION_ID": "inherited", "MEMENTO_JOURNAL": os.path.join(trap, "j")})
    return done, inherited


done, inherited = suite("""
with open(os.path.expanduser("~/written"), "w") as f:
    f.write("a file under HOME")
subprocess.run(["git", "init", "-q"], cwd=scratch, check=True)
subprocess.run(["git", "config", "probe.key", "value"], cwd=scratch, check=True)
child = subprocess.run([sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                       capture_output=True, text=True, check=True).stdout.strip()
print(json.dumps({"scratch": scratch, "environment": dict(os.environ),
                  "temp": os.path.dirname(tempfile.mkdtemp()), "child temp": child}))
""")
check("a suite calling home runs", done.returncode == 0, done.stderr)
seen = json.loads(done.stdout)
scratch = seen["scratch"]

check("its environment is PATH and the scratch directory as HOME and TMPDIR",
      seen["environment"] == {"PATH": os.environ["PATH"], "HOME": scratch, "TMPDIR": scratch},
      seen["environment"])
check("the scratch directory is named for the suite",
      os.path.basename(scratch).startswith("probe.test.py."), scratch)
check("a temp file it makes naming no directory lands in the scratch", seen["temp"] == scratch,
      seen["temp"])
check("and so does one a command it runs makes", seen["child temp"] == scratch, seen["child temp"])
check("the scratch directory is gone once it exits", not os.path.exists(scratch), scratch)
left = {name: os.listdir(path) for name, path in inherited.items()}
check("what it wrote under HOME and the repository git made are not in the HOME, TMPDIR or GIT_DIR "
      "it inherited", all(entries == [] for entries in left.values()), left)

done, inherited = suite("""
os.makedirs(os.path.join(scratch, "locked", "inside"))
os.chmod(os.path.join(scratch, "locked"), 0o500)
print(scratch)
""")
kept = done.stdout.strip()
check("a scratch directory it cannot remove fails the suite, naming it",
      done.returncode == 1 and f"cannot remove the scratch directory {kept}:" in done.stderr,
      f"rc={done.returncode} err={done.stderr!r}")
os.chmod(os.path.join(kept, "locked"), 0o700)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
