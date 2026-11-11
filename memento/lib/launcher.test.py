#!/usr/bin/env python3
"""Tests for launcher: every shape of a command that runs finalize-session names it, a mention does
too, and a name that only contains it does not.

Run with: python3 launcher.test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402

hermetic.home()
from launcher import names_launcher  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


NAMING = {
    "by its path": "/x/memento/skills/message-in-a-bottle/bin/finalize-session 'msg'",
    "by its bare name": "finalize-session /next",
    "after a variable assignment": "FINALIZE_DRY_RUN=1 finalize-session x",
    "by a quoted plugin-root path":
        '"${CLAUDE_PLUGIN_ROOT}/skills/message-in-a-bottle/bin/finalize-session" x',
    "across a backslash-newline": "lit done x && \\\n  /x/bin/finalize-session 'm'",
    "after a newline": "cd /x\nfinalize-session 'm'",
    "after `)&&`": "(cd x)&&finalize-session y",
    "after a heredoc whose body holds an apostrophe":
        "cat > /tmp/m <<'EOF'\nit's done\nEOF\nfinalize-session \"$(cat /tmp/m)\"",
    "inside a wrapper's command": "bash -c 'cd /x && finalize-session m'",
    "in a mere mention": "grep finalize-session f",
}
NOT_NAMING = {
    "the launcher's test": "python3 skills/message-in-a-bottle/bin/finalize-session.test.py",
    "a plural": "finalize-sessions",
    "an underscored suffix": "finalize-session_x",
    "a prefix": "xfinalize-session",
    "an unrelated command": "echo hello",
}

for shape, command in NAMING.items():
    check(f"a command naming the launcher {shape} names it", names_launcher(command), repr(command))
for shape, command in NOT_NAMING.items():
    check(f"{shape} does not name the launcher", not names_launcher(command), repr(command))

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
