#!/usr/bin/env python3
"""Tests for the Remote Control poster: which session a worker token names, where Claude Code keeps the
login, what a prompt looks like in the log, and how the wait for the reset ends. The HTTP calls run against
a fake session server on localhost, and the script form runs once as the launcher runs it.

Run with: python3 remote_control.test.py
"""

import http.server
import json
import os
import subprocess
import sys
import threading

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import remote_control  # noqa: E402
from remote_control import Refused  # noqa: E402

failures = []
SESSION = "cse_01N5sakbg7CFUBuSSUMs5Le1"


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def refusal(call):
    """The message `call` refuses with, or None when it returns."""
    try:
        call()
    except Refused as error:
        return str(error)
    return None


# --- the session a claude's command line names -------------------------------
# The shape `claude remote-control` starts a session's claude with (2.1.299): the session's URL as
# --sdk-url and again as --resume=, and its id as --session-id.
URL = f"https://api.anthropic.com/v1/code/sessions/{SESSION}"
SPAWNED = (f"/Users/bmf/.local/share/claude/versions/2.1.299 --print --sdk-url {URL} --session-id {SESSION} "
           f"--input-format stream-json --output-format stream-json --replay-user-messages --resume={URL} "
           f"--mcp-config=/Users/bmf/.claude/bridge-spawn/{SESSION}-doIX9g/mcp.json --model claude-opus-5-5")

check("a spawned session's command line names its session", remote_control.session_id(SPAWNED) == SESSION)
check("a claude started with no --sdk-url is refused",
      "no --sdk-url" in (refusal(lambda: remote_control.session_id("claude --permission-mode auto")) or ""))
check("a command line naming two sessions is refused, naming both",
      "cse_other" in (refusal(lambda: remote_control.session_id(
          SPAWNED + " --resume=https://api.anthropic.com/v1/code/sessions/cse_other")) or ""))

# --- where the login is kept -------------------------------------------------
# The suffix is checked against a name taken from a live keychain on 2.1.299:
# CLAUDE_CONFIG_DIR=/Users/bmf/.claude-laws-eval stores under Claude Code-credentials-336d9a5d.

check("no config dir: the bare keychain service",
      remote_control.credential_service({}) == "Claude Code-credentials")
check("a config dir suffixes the service with its SHA-256",
      remote_control.credential_service({"CLAUDE_CONFIG_DIR": "/Users/bmf/.claude-laws-eval"})
      == "Claude Code-credentials-336d9a5d")
check("the secure-storage dir overrides the config dir",
      remote_control.credential_service({"CLAUDE_CONFIG_DIR": "/elsewhere",
                                         "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/Users/bmf/.claude-laws-eval"})
      == "Claude Code-credentials-336d9a5d")
check("an empty secure-storage dir means the bare service, whatever the config dir",
      remote_control.credential_service({"CLAUDE_CONFIG_DIR": "/elsewhere",
                                         "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""})
      == "Claude Code-credentials")
check("off macOS the login is .credentials.json in the config dir",
      remote_control.credential_file({"CLAUDE_CONFIG_DIR": "/cfg"}) == "/cfg/.credentials.json")
check("the access token is read from the stored login",
      remote_control.access_token('{"claudeAiOauth": {"accessToken": "tok"}}') == "tok")
check("a stored login without an access token is refused",
      refusal(lambda: remote_control.access_token('{"mcpOAuth": {}}')))

# --- a prompt and the reset, as the log holds them ---------------------------

event = remote_control.user_event(SESSION, "/clear")["payload"]
check("a prompt is a user event naming the session in its session_ spelling",
      event["type"] == "user" and event["session_id"] == "session_01N5sakbg7CFUBuSSUMs5Le1"
      and event["message"] == {"role": "user", "content": "/clear"}, event)
check("every prompt carries its own uuid",
      event["uuid"] != remote_control.user_event(SESSION, "/clear")["payload"]["uuid"])
check("events holding the worker's conversation_reset hold a reset",
      remote_control.holds([{"payload": {"type": "result"}}, {"payload": {"type": "conversation_reset"}}],
                           "conversation_reset"))
check("events of other types, or none, do not",
      not remote_control.holds([{"payload": {"type": "user"}}, {}], "result")
      and not remote_control.holds([], "result"))


class Clock:
    """Time that moves only when the code under test sleeps."""
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


pages = iter([[], [{"payload": {"type": "assistant"}}], [{"payload": {"type": "result"}}]])
asked = []
clock = Clock()
outcome = remote_control.await_event(lambda after: asked.append(after) or next(pages), 7, "result", 60,
                                     clock, clock.sleep)
check("the wait ends as soon as the log holds the event", outcome == 0 and len(asked) == 3, (outcome, asked))
check("and every read asks for events after the given sequence", set(asked) == {7}, asked)
clock = Clock()
outcome = remote_control.await_event(lambda after: [{"payload": {"type": "result"}}], 7, "conversation_reset",
                                     100, clock, clock.sleep)
check("an event of another type never ends the wait, which ends at its timeout",
      outcome == 1 and 100 <= clock.now < 100 + remote_control.POLL_CEILING_SECONDS, (outcome, clock.now))
slept, clock = [], Clock()
remote_control.await_event(lambda after: [], 7, "result", 600, clock,
                           lambda seconds: slept.append(seconds) or clock.sleep(seconds))
check("the polling backs off, doubling up to its ceiling",
      slept[:2] == [remote_control.POLL_SECONDS, 2 * remote_control.POLL_SECONDS]
      and max(slept) == remote_control.POLL_CEILING_SECONDS, slept[:12])

# --- the session server -------------------------------------------------------
# A fake server on localhost: it records each request, numbers posted events from 40, answers a
# read with two pages, and refuses a session it was told to.

received = []


class Server(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received.append((self.path, self.headers.get("Authorization"), body,
                         self.headers.get("anthropic-client-platform")))
        if "cse_refused" in self.path:
            return self.answer(403, {"error": {"type": "permission_error", "message": "not yours"}})
        self.answer(200, {"attestation_feedback": None,
                          "results": [{"event_id": "e", "sequence_num": str(40 + len(received)), "duplicate": False}]})

    def do_GET(self):
        received.append((self.path, self.headers.get("Authorization"), None))
        if "sort_order=desc&limit=1" in self.path:
            return self.answer(200, {"data": [{"sequence_num": "43", "payload": {"type": "result"}}]})
        if "cursor=41" in self.path:
            return self.answer(200, {"data": [{"sequence_num": "42", "payload": {"type": "result"}}],
                                     "next_cursor": "42"})
        self.answer(200, {"data": [{"sequence_num": "43", "payload": {"type": "conversation_reset"}}]})


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
threading.Thread(target=server.serve_forever, daemon=True).start()
remote_control.API_URL = f"http://127.0.0.1:{server.server_address[1]}"

seq = remote_control.post("tok", SESSION, "/clear")
path, auth, body, platform = received[-1]
check("a post declares Claude Code's CLI platform, without which the session runs no slash command",
      platform == "claude_code_cli", platform)
check("a post goes to the session's events with the login as bearer",
      path == f"/v1/code/sessions/{SESSION}/events" and auth == "Bearer tok", (path, auth))
check("and carries the one prompt", [e["payload"]["message"]["content"] for e in body["events"]] == ["/clear"])
check("and returns the sequence number the log gave it", seq == 41, seq)
events = remote_control.events_after("tok", SESSION, 41)
check("a read follows next_cursor to the last page",
      [e["sequence_num"] for e in events] == ["42", "43"] and remote_control.holds(events, "conversation_reset"),
      events)
check("the head is the newest event's sequence, read as one event in descending order",
      remote_control.head("tok", SESSION) == 43 and "sort_order=desc&limit=1" in received[-1][0], received[-1])
reason = refusal(lambda: remote_control.post("tok", "cse_refused", "/clear"))
check("a refused post names the status and the server's reason",
      reason is not None and "HTTP 403" in reason and "not yours" in reason, reason)

# --- the script, as the launcher runs it ---------------------------------------

script = os.path.join(LIB, "remote_control.py")
done = subprocess.run([sys.executable, "-I", script, "session-id", SPAWNED], capture_output=True, text=True)
check("session-id prints the session", done.returncode == 0 and done.stdout.strip() == SESSION,
      (done.returncode, done.stdout, done.stderr))
done = subprocess.run([sys.executable, "-I", script, "session-id", "claude"], capture_output=True, text=True)
check("session-id for a claude with no session exits 2 with the reason",
      done.returncode == 2 and "no --sdk-url" in done.stderr, (done.returncode, done.stderr))
done = subprocess.run([sys.executable, "-I", script, "post", SESSION], capture_output=True, text=True)
check("a command missing an argument prints the usage and exits 2",
      done.returncode == 2 and "usage: remote_control.py session-id" in done.stderr, done.stderr)

server.shutdown()
print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
