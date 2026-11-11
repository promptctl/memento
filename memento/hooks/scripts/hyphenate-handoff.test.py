#!/usr/bin/env python3
"""Unit tests for the hyphenate-handoff hook. Run with `python3 hyphenate-handoff.test.py`."""

import json
import os
import subprocess
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hyphenate-handoff.py")


def rewritten(command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    result = subprocess.run([HOOK], input=payload, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"] if result.stdout else command


class HyphenateHandoff(unittest.TestCase):
    def test_banned_words_in_a_handoff_are_spelled_out(self):
        self.assertEqual(rewritten("finalize-session 'Bash and git, /usr/bin/zsh, git-lfs'"),
                         "finalize-session 'B-a-s-h and g-i-t, /usr/bin/z-s-h, g-i-t-lfs'")

    def test_words_that_only_contain_a_banned_word_are_left_alone(self):
        self.assertEqual(rewritten("finalize-session 'digit, shell, github, session'"),
                         "finalize-session 'digit, shell, github, session'")

    def test_a_rewritten_command_is_not_rewritten_again(self):
        once = rewritten("finalize-session 'git'")
        self.assertEqual(rewritten(once), once)

    def test_banned_words_in_a_journal_note_are_spelled_out(self):
        self.assertEqual(rewritten("/plugin/skills/journal/bin/journal note 'git pull, then bash -n'"),
                         "/plugin/skills/journal/bin/journal note 'g-i-t pull, then b-a-s-h -n'")
        self.assertEqual(rewritten("\"$ROOT/skills/journal/bin/journal\" note 'git'"),
                         "\"$ROOT/skills/journal/bin/journal\" note 'g-i-t'")

    def test_a_journal_verb_that_takes_no_prose_is_left_alone(self):
        self.assertEqual(rewritten("/plugin/skills/journal/bin/journal show text=git"),
                         "/plugin/skills/journal/bin/journal show text=git")

    def test_other_commands_are_left_alone(self):
        self.assertEqual(rewritten("echo git"), "echo git")
        self.assertEqual(rewritten("git add memento/skills/journal/bin/journal notes.md"),
                         "git add memento/skills/journal/bin/journal notes.md")


if __name__ == "__main__":
    unittest.main()
