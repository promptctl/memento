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
    """The reason the hook stops a call of `session_id` with, or None where it prints nothing. A stop both
    denies the call and ends the turn, with one reason shown to the model and to the user."""
    payload = json.dumps({"session_id": session_id, "tool_name": tool_name, "tool_input": {}})
    result = subprocess.run([HOOK], input=payload, capture_output=True, text=True, check=True)
    if not result.stdout:
        return None
    output = json.loads(result.stdout)
    assert output["continue"] is False, output
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny", output
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == output["stopReason"], output
    return output["stopReason"]


def script(*args, home=None):
    env = {**os.environ, "HOME": home} if home else None
    return subprocess.run([sys.executable, "-I", os.path.join(LIB, "handed_off.py"), *args],
                          capture_output=True, text=True, env=env)


class Fence(unittest.TestCase):
    def test_a_session_that_handed_off_has_its_turn_stopped_at_any_tool_call_naming_its_successor(self):
        marked = script("mark", "old-1", "its successor is session new-1.")
        self.assertEqual(marked.returncode, 0, marked.stderr)
        self.assertEqual(marked.stdout.strip(), handed_off.path("old-1"))
        for tool in ("Bash", "Edit", "mcp__github__create_pull_request"):
            reason = decided("old-1", tool)
            self.assertIn("its successor is session new-1.", reason)
            self.assertIn(handed_off.path("old-1"), reason)

    def test_a_session_with_no_record_is_left_to_the_normal_permission_flow(self):
        script("mark", "old-2", "its successor is session new-2.")
        self.assertIsNone(decided("another-session"))

    def test_a_cleared_record_lifts_the_fence_and_clearing_none_is_not_an_error(self):
        script("mark", "old-3", "its successor is session new-3.")
        self.assertEqual(script("clear", "old-3").returncode, 0)
        self.assertIsNone(decided("old-3"))
        self.assertEqual(script("clear", "old-3").returncode, 0)

    def test_a_record_that_stands_but_cannot_be_read_still_stops_naming_why(self):
        script("mark", "old-5", "its successor is session new-5.")
        os.chmod(handed_off.path("old-5"), 0)
        try:
            reason = decided("old-5")
        finally:
            os.chmod(handed_off.path("old-5"), 0o600)
        self.assertIn("could not be read", reason)
        self.assertIn(handed_off.path("old-5"), reason)

    def test_a_mark_that_cannot_be_written_fails_naming_why(self):
        # A HOME of its own, where a file stands where the records' directory goes.
        home = tempfile.mkdtemp()
        blocked = os.path.join(home, ".claude", "memento", "handed-off")
        os.makedirs(os.path.dirname(blocked))
        open(blocked, "w").close()
        marked = script("mark", "old-4", "its successor is session new-4.", home=home)
        self.assertEqual(marked.returncode, 2)
        self.assertIn(blocked, marked.stderr)

    def test_a_mark_whose_write_fails_leaves_no_record_to_fence_on(self):
        # A directory where the staged file goes fails the write before anything is renamed.
        os.makedirs(handed_off.path("old-6") + ".staged")
        marked = script("mark", "old-6", "its successor is session new-6.")
        self.assertEqual(marked.returncode, 2)
        self.assertIsNone(decided("old-6"))


if __name__ == "__main__":
    unittest.main()
