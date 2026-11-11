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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hermetic  # noqa: E402

SCRATCH = hermetic.home()

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


PROBE = """import json, os, subprocess, sys, tempfile
sys.path.insert(0, sys.argv[1])
import hermetic
scratch = hermetic.home()
child = subprocess.run([sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                       capture_output=True, text=True, check=True).stdout.strip()
print(json.dumps({"scratch": scratch, "environment": dict(os.environ),
                  "temp": os.path.dirname(tempfile.mkdtemp()), "child temp": child}))
"""

trap = os.path.join(SCRATCH, "trap")
inherited = {name: os.path.join(trap, name.lower()) for name in ("HOME", "TMPDIR", "GIT_DIR")}
for path in inherited.values():
    os.makedirs(path)
probe = os.path.join(trap, "probe.test.py")
with open(probe, "w") as f:
    f.write(PROBE)
done = subprocess.run([sys.executable, probe, HERE], capture_output=True, text=True,
                      env={**inherited, "PATH": os.environ["PATH"], "GIT_WORK_TREE": trap,
                           "CLAUDE_CODE_SESSION_ID": "inherited", "MEMENTO_JOURNAL": os.path.join(trap, "j")})
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
check("nothing is left in the HOME, TMPDIR or GIT_DIR it inherited",
      all(entries == [] for entries in left.values()), left)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
