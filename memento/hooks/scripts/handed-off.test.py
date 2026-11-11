#!/usr/bin/env python3
"""Unit tests for the handed-off hook and the record it reads (lib/handed_off.py). Run with
`python3 handed-off.test.py`."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "handed-off.py")
LIB = os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib")
sys.path.insert(0, LIB)
import hermetic  # noqa: E402

hermetic.home()
import handed_off  # noqa: E402


def decided(session_id, tool_name="Bash"):
    """The hook's hookSpecificOutput for a call of `session_id`, or None where it prints nothing."""
    payload = json.dumps({"session_id": session_id, "tool_name": tool_name, "tool_input": {}})
    result = subprocess.run([HOOK], input=payload, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else None


def script(*args, home=None):
    env = {**os.environ, "HOME": home} if home else None
    return subprocess.run([sys.executable, "-I", os.path.join(LIB, "handed_off.py"), *args],
                          capture_output=True, text=True, env=env)


class Fence(unittest.TestCase):
    def test_a_session_that_handed_off_has_every_tool_call_denied_naming_its_successor(self):
        marked = script("mark", "old-1", "its successor is session new-1.")
        self.assertEqual(marked.returncode, 0, marked.stderr)
        self.assertEqual(marked.stdout.strip(), handed_off.path("old-1"))
        for tool in ("Bash", "Edit", "mcp__github__create_pull_request"):
            output = decided("old-1", tool)
            self.assertEqual(output["permissionDecision"], "deny")
            self.assertIn("its successor is session new-1.", output["permissionDecisionReason"])
            self.assertIn(handed_off.path("old-1"), output["permissionDecisionReason"])

    def test_a_session_with_no_record_is_left_to_the_normal_permission_flow(self):
        script("mark", "old-2", "its successor is session new-2.")
        self.assertIsNone(decided("another-session"))

    def test_a_cleared_record_lifts_the_fence_and_clearing_none_is_not_an_error(self):
        script("mark", "old-3", "its successor is session new-3.")
        self.assertEqual(script("clear", "old-3").returncode, 0)
        self.assertIsNone(decided("old-3"))
        self.assertEqual(script("clear", "old-3").returncode, 0)

    def test_a_mark_that_cannot_be_written_fails_naming_why(self):
        # A HOME of its own, where a file stands where the records' directory goes.
        home = tempfile.mkdtemp()
        blocked = os.path.join(home, ".claude", "memento", "handed-off")
        os.makedirs(os.path.dirname(blocked))
        open(blocked, "w").close()
        marked = script("mark", "old-4", "its successor is session new-4.", home=home)
        self.assertEqual(marked.returncode, 2)
        self.assertIn(blocked, marked.stderr)


if __name__ == "__main__":
    unittest.main()
