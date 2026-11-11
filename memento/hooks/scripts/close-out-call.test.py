#!/usr/bin/env python3
"""Unit tests for the close-out-call hook. Run with `python3 close-out-call.test.py`."""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "close-out-call.py")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
import hermetic  # noqa: E402

hermetic.home()


def decided(tool_input):
    """The hook's hookSpecificOutput for a Bash call with `tool_input`, or None where it prints nothing."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": tool_input})
    result = subprocess.run([HOOK], input=payload, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else None


def rewritten(command):
    output = decided({"command": command})
    return output["updatedInput"]["command"] if output else command


def backgrounded(command):
    return decided({"command": command, "run_in_background": True})


class Rewrite(unittest.TestCase):
    def test_banned_words_in_a_handoff_are_spelled_out(self):
        self.assertEqual(rewritten("finalize-session 'Bash and git, /usr/bin/zsh, git-lfs'"),
                         "finalize-session 'B-a-s-h and g-i-t, /usr/bin/z-s-h, g-i-t-lfs'")

    def test_words_that_only_contain_a_banned_word_are_left_alone(self):
        self.assertEqual(rewritten("finalize-session 'digit, shell, github, session'"),
                         "finalize-session 'digit, shell, github, session'")

    def test_a_rewritten_command_is_not_rewritten_again(self):
        once = rewritten("finalize-session 'git'")
        self.assertEqual(rewritten(once), once)

    def test_other_commands_are_left_alone(self):
        self.assertEqual(rewritten("echo git"), "echo git")

    def test_what_comes_before_the_launcher_is_left_alone(self):
        self.assertEqual(rewritten("/x/journal summary --file /tmp/git-summary.md && /git/bin/finalize-session 'git'"),
                         "/x/journal summary --file /tmp/git-summary.md && /git/bin/finalize-session 'g-i-t'")
        self.assertEqual(rewritten("git log --grep finalize-session"), "git log --grep finalize-session")

    def test_a_rewrite_returns_the_other_fields_unchanged(self):
        given = {"command": "finalize-session 'git'", "timeout": 30000,
                 "description": "Close out", "run_in_background": False}
        output = decided(given)
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertEqual(output["updatedInput"], {**given, "command": "finalize-session 'g-i-t'"})


class ForegroundGate(unittest.TestCase):
    SHAPES = {
        "a bare call": "finalize-session 'm'",
        "after FINALIZE_DRY_RUN=1": "FINALIZE_DRY_RUN=1 finalize-session 'm'",
        "joined to lit by &&": "lit done x && /x/skills/message-in-a-bottle/bin/finalize-session 'm'",
        "across a backslash-newline": "lit done x && \\\n  /x/bin/finalize-session 'm'",
        "after a newline": "cd /x\nfinalize-session 'm'",
        "after (cd x)&&": "(cd x)&&finalize-session 'm'",
        "after a heredoc whose body holds an apostrophe":
            "cat > /tmp/m <<'EOF'\nit's done\nEOF\nfinalize-session \"$(cat /tmp/m)\"",
        "a mere mention": "grep finalize-session f",
    }

    def test_a_backgrounded_call_naming_the_launcher_is_denied_in_every_shape(self):
        for shape, command in self.SHAPES.items():
            with self.subTest(shape):
                output = backgrounded(command)
                self.assertEqual(output["permissionDecision"], "deny")
                self.assertIn("foreground", output["permissionDecisionReason"])
                self.assertNotIn("updatedInput", output)

    def test_the_launchers_test_is_neither_denied_nor_rewritten(self):
        command = "python3 skills/message-in-a-bottle/bin/finalize-session.test.py && echo git"
        self.assertIsNone(backgrounded(command))
        self.assertIsNone(decided({"command": command}))


if __name__ == "__main__":
    unittest.main()
