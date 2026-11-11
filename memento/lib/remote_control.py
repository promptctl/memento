#!/usr/bin/env python3
"""Posts prompts into a Claude Code Remote Control session through its event log on the session server.

A session that `claude remote-control` spawns is a `claude --print` child whose input is the server's
pipe, so nothing local can type into it. Its prompts arrive as client events in the session's log,
`/v1/code/sessions/<cse id>/events`, which is where claude.ai posts them; this posts them there the same
way, under the user's own claude.ai login. Run as a script:

  remote_control.py session-id <command line>
      prints the cse_ id of the session a claude's command line names in its --sdk-url.
  remote_control.py head <session>
      prints the sequence number of the last event in the session's log.
  remote_control.py post <session> <text>
      posts <text> as a user prompt and prints the sequence number the log gave it.
  remote_control.py await <session> <after seq> <event type> <timeout seconds>
      exits 0 once the log holds an event of that type after <after seq>, and 1 at the timeout.

Each prints why it cannot answer on stderr and exits 2.

A prompt posted mid-turn can be delivered into the running turn at its next tool call, as text, so a
slash command posted then is read as words and never run (2.1.299). Post one only while the session is
idle, which a `result` event in the log marks.
"""

import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
import uuid

# The session server, overridable so a test can stand up its own.
API_URL = os.environ.get("MEMENTO_REMOTE_API_URL", "https://api.anthropic.com")
# A wait polls the log at POLL_SECONDS, doubling up to POLL_CEILING_SECONDS, so a wait of seconds ends
# within seconds and one that lasts a long turn makes few requests. The first is overridable for tests.
POLL_SECONDS = float(os.environ.get("MEMENTO_REMOTE_POLL_SECONDS", "1"))
POLL_CEILING_SECONDS = 30
# The session's URL on the server, which the server passes the claude it spawns as --sdk-url (and again as
# --resume=). A tool call sees neither the id nor the worker token naming it: its CLAUDE_CODE_SESSION_ID
# is a local uuid, and Claude Code strips the token from a bridge child's tools (2.1.299).
SESSION_URL = re.compile(r"/v1/code/sessions/(cse_[A-Za-z0-9]+)")


class Refused(Exception):
    """The session cannot be reached or did not answer as the log promises; the message says why."""


def session_id(command_line):
    """The cse_ id of the one session a claude's command line names."""
    found = set(SESSION_URL.findall(command_line))
    if len(found) != 1:
        raise Refused(f"the claude's command line names {len(found)} Remote Control sessions "
                      f"({', '.join(sorted(found)) or 'no --sdk-url'}), not one: {command_line[:300]}")
    return found.pop()


def credential_service(environ):
    """The keychain service Claude Code stores the login under: `Claude Code-credentials`, suffixed with
    the first 8 hex digits of the config dir's SHA-256 when a config dir is set (2.1.299)."""
    secure = environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    directory = secure if secure is not None else environ.get("CLAUDE_CONFIG_DIR", "")
    if not directory:
        return "Claude Code-credentials"
    digest = hashlib.sha256(unicodedata.normalize("NFC", directory).encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def credential_file(environ):
    """Where Claude Code stores the login off macOS: `.credentials.json` in the same directory."""
    secure = environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    directory = (secure if secure is not None else environ.get("CLAUDE_CONFIG_DIR")) \
        or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(directory, ".credentials.json")


def stored_credential(environ):
    """The login Claude Code stored, as its JSON text. [LAW:effects-at-boundaries] The one read of it."""
    if sys.platform == "darwin":
        service = credential_service(environ)
        read = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                              capture_output=True, text=True)
        if read.returncode != 0:
            raise Refused(f"no claude.ai login in the keychain under '{service}': {read.stderr.strip()}")
        return read.stdout
    path = credential_file(environ)
    try:
        with open(path) as handle:
            return handle.read()
    except OSError as error:
        raise Refused(f"no claude.ai login at {path}: {error}") from None


def access_token(stored):
    """The OAuth access token in the stored login. Claude Code refreshes it, so an expired one surfaces as
    the server's 401, not here."""
    try:
        return json.loads(stored)["claudeAiOauth"]["accessToken"]
    except (ValueError, KeyError, TypeError):
        raise Refused("the stored claude.ai login holds no OAuth access token") from None


def user_event(session, text):
    """A user prompt as the log takes it. The payload's session_id uses the session_ spelling, as
    claude.ai's does."""
    return {"payload": {"type": "user", "uuid": str(uuid.uuid4()),
                        "session_id": "session_" + session[len("cse_"):], "parent_tool_use_id": None,
                        "message": {"role": "user", "content": text}}}


def holds(events, kind):
    """Whether log events hold one of this type: `result` ends a turn, and `conversation_reset` is a
    /clear having reset the session."""
    return any((event.get("payload") or {}).get("type") == kind for event in events)


def request(token, method, path, body=None):
    """One call to the session server. [LAW:effects-at-boundaries] Every request goes through here, and
    any answer but 2xx is a refusal naming the status and the server's reason.

    The platform is the one Claude Code's own CLI declares when it posts to a session. The session reads
    a prompt from a request that declares none as a message from an unknown peer, and runs no slash
    command in it: an idle session took a posted /clear as text (2.1.299)."""
    data = None if body is None else json.dumps(body).encode()
    call = urllib.request.Request(API_URL + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "anthropic-version": "2023-06-01", "anthropic-beta": "ccr-byoc-2025-07-29",
        "anthropic-client-platform": "claude_code_cli"})
    try:
        with urllib.request.urlopen(call, timeout=30) as answer:
            return json.loads(answer.read())
    except urllib.error.HTTPError as error:
        raise Refused(f"{method} {path} answered HTTP {error.code}: "
                      f"{error.read().decode(errors='replace')[:400]}") from None
    except urllib.error.URLError as error:
        raise Refused(f"{method} {path} failed: {error.reason}") from None


def post(token, session, text):
    """Appends a user prompt to the session's log and returns the sequence number it was given."""
    answer = request(token, "POST", f"/v1/code/sessions/{session}/events",
                     {"events": [user_event(session, text)]})
    try:
        return int(answer["results"][0]["sequence_num"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise Refused(f"the log took the prompt but returned no sequence number: {answer!r}") from None


def head(token, session):
    """The sequence number of the last event in the session's log."""
    page = request(token, "GET", f"/v1/code/sessions/{session}/events?sort_order=desc&limit=1")
    try:
        return int(page["data"][0]["sequence_num"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise Refused(f"session {session}'s log answered with no last event: {page!r}") from None


def events_after(token, session, after):
    """Every log event after sequence number `after`, oldest first, across pages."""
    events, cursor = [], after
    while True:
        page = request(token, "GET", f"/v1/code/sessions/{session}/events?sort_order=asc&cursor={cursor}")
        events += page.get("data", [])
        if not page.get("next_cursor"):
            return events
        cursor = page["next_cursor"]


def await_event(fetch, after, kind, timeout, clock=time.monotonic, sleep=time.sleep):
    """Polls `fetch(after)` until it holds an event of type `kind`: 0 when it does, 1 at the timeout.
    [LAW:no-ambient-temporal-coupling] The session's own log is the one signal of where it stands."""
    deadline, interval = clock() + timeout, POLL_SECONDS
    while not holds(fetch(after), kind):
        if clock() >= deadline:
            return 1
        sleep(interval)
        interval = min(interval * 2, POLL_CEILING_SECONDS)
    return 0


def _session_id(command_line):
    print(session_id(command_line))
    return 0


def _head(session):
    print(head(access_token(stored_credential(os.environ)), session))
    return 0


def _post(session, text):
    print(post(access_token(stored_credential(os.environ)), session, text))
    return 0


def _await(session, after_seq, event_type, timeout_seconds):
    token = access_token(stored_credential(os.environ))
    return await_event(lambda after: events_after(token, session, after), int(after_seq), event_type,
                       float(timeout_seconds))


COMMANDS = {"session-id": _session_id, "head": _head, "post": _post, "await": _await}


def _parameters(command):
    return list(inspect.signature(command).parameters)


if __name__ == "__main__":
    # [LAW:one-source-of-truth] Each command's signature is both its arity and its usage line.
    command, arguments = COMMANDS.get(sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if command is None or len(arguments) != len(_parameters(command)):
        print("usage: " + " | ".join(
            f"remote_control.py {name} " + " ".join(f"<{p.replace('_', ' ')}>" for p in _parameters(fn))
            for name, fn in COMMANDS.items()), file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(command(*arguments))
    except Refused as error:
        print(error, file=sys.stderr)
        sys.exit(2)
    # Python exits 1 on an uncaught exception, which await-reset would read as a timeout, so a crash
    # says so with 2 and the traceback.
    except Exception:
        traceback.print_exc()
        sys.exit(2)
