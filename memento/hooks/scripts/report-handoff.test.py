#!/usr/bin/env python3
"""Unit tests for the report-handoff hook. Run with `python3 report-handoff.test.py`."""

import json
import os
import subprocess
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report-handoff.py")
SESSION = "1b2c3d4e-0000-4000-8000-000000000000"


class ReportHandoff(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="report-handoff.")
        self.report = os.path.join(self.home, ".claude", "memento", "reports", SESSION)

    def run_hook(self, command, **fields):
        """Run the hook on a PreToolUse(Bash) payload; return what it printed."""
        payload = json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": command}, **fields})
        return subprocess.run([HOOK], input=payload, capture_output=True, text=True, check=True,
                              env={"PATH": os.environ["PATH"], "HOME": self.home}).stdout

    def reported(self):
        with open(self.report) as handle:
            return handle.read()

    def test_a_handoff_reports_the_session_mode_and_effort(self):
        self.run_hook("finalize-session 'next'", permission_mode="bypassPermissions", effort={"level": "high"})
        self.assertEqual(self.reported(), "bypassPermissions\nhigh\n")

    def test_a_payload_without_mode_or_effort_reports_them_empty(self):
        self.run_hook("finalize-session 'next'")
        self.assertEqual(self.reported(), "\n\n")

    def test_a_later_handoff_replaces_the_report(self):
        self.run_hook("finalize-session 'next'", permission_mode="bypassPermissions")
        self.run_hook("finalize-session 'next'", permission_mode="acceptEdits")
        self.assertEqual(self.reported(), "acceptEdits\n\n")

    def test_a_report_prints_nothing(self):
        # Approval and rewriting belong to hyphenate-handoff; reporting neither approves nor rewrites.
        self.assertEqual(self.run_hook("finalize-session 'git'", permission_mode="default"), "")

    def test_other_commands_write_no_report(self):
        self.run_hook("echo hi", permission_mode="default")
        self.assertEqual(os.listdir(self.home), [])


if __name__ == "__main__":
    unittest.main()
