#!/usr/bin/env python3
"""Unit tests for the prepare-handoff hook. Run with `python3 prepare-handoff.test.py`."""

import json
import os
import subprocess
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prepare-handoff.py")
SESSION = "1b2c3d4e-0000-4000-8000-000000000000"


def run_hook(command, tmpdir, **fields):
    """Run the hook on a PreToolUse(Bash) payload; return (the command Claude Code would run, its output)."""
    payload = json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": command}, **fields})
    result = subprocess.run([HOOK], input=payload, capture_output=True, text=True, check=True,
                            env={**os.environ, "TMPDIR": tmpdir})
    output = json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else None
    return (output["updatedInput"]["command"] if output else command), output


class PrepareHandoff(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="prepare-handoff.")
        self.report = os.path.join(self.tmpdir, f"memento-report.{SESSION}")

    def rewritten(self, command):
        return run_hook(command, self.tmpdir, permission_mode="default")[0]

    def test_banned_words_in_a_handoff_are_spelled_out(self):
        self.assertEqual(self.rewritten("finalize-session 'Bash and git, /usr/bin/zsh, git-lfs'"),
                         "finalize-session 'B-a-s-h and g-i-t, /usr/bin/z-s-h, g-i-t-lfs'")

    def test_words_that_only_contain_a_banned_word_are_left_alone(self):
        self.assertEqual(self.rewritten("finalize-session 'digit, shell, github, session'"),
                         "finalize-session 'digit, shell, github, session'")

    def test_a_rewritten_command_is_not_rewritten_again(self):
        once = self.rewritten("finalize-session 'git'")
        self.assertEqual(self.rewritten(once), once)

    def test_other_commands_are_left_alone(self):
        self.assertEqual(self.rewritten("echo git"), "echo git")

    def test_a_handoff_reports_the_session_mode_and_effort(self):
        run_hook("finalize-session 'next'", self.tmpdir, permission_mode="bypassPermissions", effort={"level": "high"})
        with open(self.report) as handle:
            self.assertEqual(handle.read(), "bypassPermissions\nhigh\n")

    def test_a_payload_without_mode_or_effort_reports_them_empty(self):
        run_hook("finalize-session 'next'", self.tmpdir)
        with open(self.report) as handle:
            self.assertEqual(handle.read(), "\n\n")

    def test_a_report_approves_nothing(self):
        # Approval stays tied to the hyphenation: a command it does not change is not approved.
        self.assertIsNone(run_hook("finalize-session 'next'", self.tmpdir, permission_mode="default")[1])

    def test_other_commands_write_no_report(self):
        run_hook("echo hi", self.tmpdir, permission_mode="default")
        self.assertEqual(os.listdir(self.tmpdir), [])


if __name__ == "__main__":
    unittest.main()
