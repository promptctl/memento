#!/usr/bin/env python3
"""Posts prompts into a Claude Code Remote Control session through its event log on the session server.

A session that `claude remote-control` spawns is a `claude --print` child whose input is the server's
pipe, so nothing local can type into it. Its prompts arrive as client events in the session's log,
`/v1/code/sessions/<cse id>/events`, which is where claude.ai posts them; this posts them there the same
way, under the user's own claude.ai login. Run as a script:

  remote_control.py session <command line>
      prints the URL of the session a claude's command line names in its --sdk-url, or nothing when it
      names none.
  remote_control.py head <session>
      prints the sequence number of the last event in the session's log.
  remote_control.py deliver <session> <after seq> <msgfile> <goalfile> <idle timeout> <reset timeout> [<setting>...]
      posts /clear, each setting, the handoff and then the goal, each once the log shows the session idle,
      logging each step on stdout. Exits 1, saying what was not posted, when one could not be.

Each prints why it cannot answer on stderr and exits 2.

A prompt posted mid-turn can be delivered into the running turn at its next tool call, as text, so a
slash command posted then is read as words and never run (2.1.299). Post one only while the session is
idle, which a `result` after the last prompt marks.
"""

import fcntl
import hashlib
import http.client
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

# A wait polls the log at POLL_SECONDS, doubling up to POLL_CEILING_SECONDS. The ceiling is short because a
# prompt typed on claude.ai between a turn's end and the next post turns that post into text in its turn.
POLL_SECONDS = 0.5
POLL_CEILING_SECONDS = 5
# The session's URL on the server, which the server passes the claude it spawns as --sdk-url (and again as
# --resume=). A tool call sees neither the id nor the worker token naming it: its CLAUDE_CODE_SESSION_ID
# is a local uuid, and Claude Code strips the token from a bridge child's tools (2.1.299).
# [LAW:one-source-of-truth] The URL also names the server, so the login goes where the session's own
# claude goes. Only the --sdk-url value is read, since the command line also holds any prompt the claude
# was started with, which may quote a URL.
SESSION_URL = re.compile(r"(?:^|\s)--sdk-url[=\s](https?://[^\s/]+/v1/code/sessions/cse_[A-Za-z0-9]+)(?=\s|$)")


class Refused(Exception):
    """The session cannot be reached or did not answer as the log promises; the message says why."""


class Transient(Refused):
    """No answer came, or one the server marks as passing (429, 5xx), so a later try may succeed."""


class Withheld(Exception):
    """A prompt was not posted, so nothing after it was either: (what, why)."""


def session_url(command_line):
    """The URL of the one session a claude's command line names, or None when it names none."""
    found = set(SESSION_URL.findall(command_line))
    if len(found) > 1:
        raise Refused(f"the claude's command line gives {len(found)} Remote Control sessions as --sdk-url "
                      f"({', '.join(sorted(found))}), not one: {command_line[:300]}")
    return found.pop() if found else None


def credential_directory(environ):
    """The config dir Claude Code names its stored login by, or None for its default: the secure-storage
    dir when it is set, even empty, else CLAUDE_CONFIG_DIR (2.1.299)."""
    secure = environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    return (secure if secure is not None else environ.get("CLAUDE_CONFIG_DIR")) or None


def credential_service(environ):
    """The keychain service the login is stored under: `Claude Code-credentials`, suffixed with the first
    8 hex digits of the config dir's SHA-256 when there is one."""
    directory = credential_directory(environ)
    if directory is None:
        return "Claude Code-credentials"
    digest = hashlib.sha256(unicodedata.normalize("NFC", directory).encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def credential_file(environ):
    """Where the login is stored off macOS: `.credentials.json` in the config dir."""
    directory = credential_directory(environ) or os.path.join(os.path.expanduser("~"), ".claude")
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
    """The OAuth access token in the stored login."""
    try:
        return json.loads(stored)["claudeAiOauth"]["accessToken"]
    except (ValueError, KeyError, TypeError):
        raise Refused("the stored claude.ai login holds no OAuth access token") from None


class Login:
    """The user's claude.ai login. Claude Code refreshes the stored token while a wait here can last a
    day, so a 401 is answered by reading the stored login again."""

    def __init__(self, read):
        self.read = read
        self.token = access_token(read())

    def refresh(self):
        self.token = access_token(self.read())


def user_event(session_id, text):
    """A user prompt as the log takes it. The payload's session_id uses the session_ spelling, as
    claude.ai's does."""
    return {"payload": {"type": "user", "uuid": str(uuid.uuid4()),
                        "session_id": "session_" + session_id[len("cse_"):], "parent_tool_use_id": None,
                        "message": {"role": "user", "content": text}}}


class Session:
    """One session on the session server, reached under the login. [LAW:effects-at-boundaries] Every
    request goes through `request`."""

    def __init__(self, url, login):
        self.url, self.login = url, login
        self.id = url.rpartition("/")[2]

    def request(self, method, path, body=None):
        """One call, answered as JSON. A 401 is retried once under a re-read login. Any other answer but
        2xx is a refusal naming the status and the server's reason, and Transient when a retry may pass.

        The platform is the one Claude Code's own CLI declares when it posts to a session. The session reads
        a prompt from a request that declares none as a message from an unknown peer, and runs no slash
        command in it: an idle session took a posted /clear as text (2.1.299)."""
        data = None if body is None else json.dumps(body).encode()
        for attempt in (1, 2):
            call = urllib.request.Request(self.url + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.login.token}", "Content-Type": "application/json",
                "anthropic-version": "2023-06-01", "anthropic-beta": "ccr-byoc-2025-07-29",
                "anthropic-client-platform": "claude_code_cli"})
            try:
                with urllib.request.urlopen(call, timeout=30) as answer:
                    text = answer.read()
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 1:
                    self.login.refresh()
                    continue
                reason = (f"{method} {self.url}{path} answered HTTP {error.code}: "
                          f"{error.read().decode(errors='replace')[:400]}")
                raise (Transient if error.code == 429 or error.code >= 500 else Refused)(reason) from None
            # A refused connection, a timeout, a reset, a body cut short: no whole answer.
            except (OSError, http.client.HTTPException) as error:
                raise Transient(f"{method} {self.url}{path} got no answer: {error!r}") from None
            try:
                return json.loads(text)
            except ValueError:
                raise Refused(f"{method} {self.url}{path} answered with no JSON: {text[:400]!r}") from None

    def post(self, event):
        """Appends an event to the log and returns the sequence number it was given."""
        answer = self.request("POST", "/events", {"events": [event]})
        try:
            return int(answer["results"][0]["sequence_num"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise Refused(f"the log took the prompt but returned no sequence number: {answer!r}") from None

    def head(self):
        """The sequence number of the last event in the log."""
        page = self.request("GET", "/events?sort_order=desc&limit=1")
        try:
            return int(page["data"][0]["sequence_num"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise Refused(f"session {self.id}'s log answered with no last event: {page!r}") from None

    def events_after(self, cursor):
        """Every log event after sequence number `cursor`, oldest first, across pages. A page's next_cursor
        is the server's token, passed back as it came."""
        events = []
        while True:
            page = self.request("GET", f"/events?sort_order=asc&cursor={urllib.parse.quote(str(cursor), safe='')}")
            events += page.get("data", [])
            if not page.get("next_cursor"):
                return events
            if str(page["next_cursor"]) == str(cursor):
                raise Refused(f"session {self.id}'s log gave back the cursor it was read at: {cursor!r}")
            cursor = page["next_cursor"]


class Log:
    """What a session's log shows, read forward from a sequence number taken during the asking turn.
    [LAW:one-source-of-truth] The log is where the session stands; this keeps only how far it has read.

    A turn starts at a `user` event and ends at its `result`, so the session is idle once a `result`
    follows the last `user` event read."""

    def __init__(self, session, after):
        self.session, self.cursor = session, after
        self.idle = False
        self.last_reset = 0  # sequence numbers start above 0
        self.prompts = {}  # a posted prompt's uuid -> its sequence number

    def read(self):
        for event in self.session.events_after(self.cursor):
            try:
                seq, payload = int(event["sequence_num"]), event.get("payload") or {}
            except (KeyError, TypeError, ValueError, AttributeError):
                raise Refused(f"session {self.session.id}'s log holds an event with no sequence number: "
                              f"{event!r}"[:400]) from None
            kind = payload.get("type")
            if kind == "user":
                self.idle = False
                self.prompts.setdefault(payload.get("uuid"), seq)
            elif kind == "result":
                self.idle = True
            elif kind == "conversation_reset":
                self.last_reset = seq
            self.cursor = seq


def retry(attempt, timeout, clock=time.monotonic, sleep=time.sleep):
    """Calls `attempt()` until it returns something other than None, and returns that, or None at the
    timeout. Between calls it waits POLL_SECONDS, doubling up to POLL_CEILING_SECONDS."""
    deadline, interval = clock() + timeout, POLL_SECONDS
    while True:
        outcome = attempt()
        if outcome is not None or clock() >= deadline:
            return outcome
        sleep(interval)
        interval = min(interval * 2, POLL_CEILING_SECONDS)


def read(log, note):
    """Reads the log on. A read that failed in passing is noted and left for the next, which may pass; a
    refusal propagates."""
    try:
        log.read()
    except Transient as error:
        note(f"reading the log failed, retrying: {error}")


def await_state(log, settled, timeout, note, clock=time.monotonic, sleep=time.sleep):
    """Reads the log until `settled(log)`: True once it is, False at the timeout.
    [LAW:no-ambient-temporal-coupling] The session's own log is the one signal of where it stands."""
    return retry(lambda: read(log, note) or settled(log) or None, timeout, clock, sleep) is not None


def post(session, log, text, timeout, note, clock=time.monotonic, sleep=time.sleep):
    """Posts a prompt and returns its sequence number. A post that failed in passing may still have landed,
    so before it is sent again the log settles it: the prompt's uuid in the log is the post having landed.
    It is sent again as the same event, uuid and all."""
    event = user_event(session.id, text)
    posted, failures = event["payload"]["uuid"], []

    def attempt():
        try:
            return session.post(event)
        except Transient as error:
            note(f"posting failed, reading the log for it before posting again: {error}")
            failures.append(error)
        read(log, note)
        return log.prompts.get(posted)

    seq = retry(attempt, timeout, clock, sleep)
    if seq is None:
        raise Refused(f"posting failed and the log never showed the prompt: {failures[-1]}")
    return seq


def exclusive(session_id, note):
    """Takes this machine's lock on posting into the session, held until the process exits.
    [LAW:no-ambient-temporal-coupling] An earlier handoff's worker waiting to post its carried goal and the
    next agent's worker waiting to post /clear wait on the same `result`, and whichever posts second is
    handed to the first one's turn as text. So workers on one session post one at a time, and each reads
    the session's state only once it holds the lock."""
    handle = open(os.path.join(tempfile.gettempdir(), f"memento-remote-{session_id}.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        note("an earlier handoff's worker is posting into this session; waiting for it to finish")
        fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def deliver(session, log, handoff, goal, settings, idle_timeout, reset_timeout, note):
    """Posts /clear, each setting, the handoff and then the goal, each once the log shows the session idle,
    or raises Withheld naming the first prompt it could not post.

    The /clear waits for the asking turn to end, and the handoff for the reset it brings. A command has
    finished when the log records its `result`, so the next is posted to an idle session. The goal waits
    for the handoff's turn to end, since `/goal` starts a turn of its own: the session runs it after the
    handoff's turn has used the message. A wait whose length is the agent's work is bounded by
    `idle_timeout`, one for a command's effect by `reset_timeout`."""
    def settle(settled, timeout, what, why):
        try:
            if await_state(log, settled, timeout, note):
                return
        except Refused as error:
            raise Withheld(what, f"cannot read the log: {error}") from None
        raise Withheld(what, f"{why} within {timeout:g}s")

    def send(text, what):
        try:
            seq = post(session, log, text, reset_timeout, note)
        except Refused as error:
            raise Withheld(what, f"could not post it: {error}") from None
        note(f"posted {what} (log sequence {seq})")
        return seq

    def idle_after(seq):
        return lambda log: log.cursor >= seq and log.idle

    settle(idle_after(log.cursor), idle_timeout, "the /clear", "the asking turn did not end")
    seq = send("/clear", "the /clear")
    settle(lambda log: log.last_reset > seq, reset_timeout, "the handoff", "the log recorded no conversation_reset")
    settle(idle_after(seq), reset_timeout, "the handoff", "the /clear did not finish")
    # The requested /model and /effort go in before the handoff, so its first turn already runs on them.
    for setting in settings:
        seq = send(setting, setting)
        settle(idle_after(seq), reset_timeout, "the handoff", f"{setting} did not finish")
    seq = send(handoff, "the handoff")
    if goal:
        settle(idle_after(seq), idle_timeout, "the carried goal", "the handoff's turn did not end")
        send(f"/goal {goal}", "the carried goal")


def _note(line):
    print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)


def _login():
    return Login(lambda: stored_credential(os.environ))


def _session(command_line):
    print(session_url(command_line) or "")
    return 0


def _head(session):
    print(Session(session, _login()).head())
    return 0


def _deliver(session, after_seq, msgfile, goalfile, idle_timeout, reset_timeout, *setting):
    _note(f"waking; remote-control session {session}, log head {after_seq}")
    try:
        lock = exclusive(session.rpartition("/")[2], _note)  # noqa: F841 - held until exit
        with open(msgfile) as handle:
            handoff = handle.read()
        with open(goalfile) as handle:
            goal = handle.read()
        remote = Session(session, _login())
        deliver(remote, Log(remote, int(after_seq)), handoff, goal, setting, float(idle_timeout),
                float(reset_timeout), _note)
    except Withheld as error:
        what, why = error.args
    # Before the first post: no lock, no handoff or goal to read, or no login.
    except (Refused, OSError) as error:
        what, why = "the /clear", str(error)
    else:
        _note("delivered")
        return 0
    _note(f"ERROR: {why}; {what} was NOT posted, nor anything after it. The handoff is preserved at {msgfile}")
    return 1


COMMANDS = {"session": _session, "head": _head, "deliver": _deliver}


def _usage(name, command):
    return f"remote_control.py {name} " + " ".join(
        f"[<{p.name}>...]" if p.kind is p.VAR_POSITIONAL else f"<{p.name.replace('_', ' ')}>"
        for p in inspect.signature(command).parameters.values())


def _takes(command, count):
    parameters = inspect.signature(command).parameters.values()
    fixed = sum(p.kind is not p.VAR_POSITIONAL for p in parameters)
    return count == fixed or (count > fixed and any(p.kind is p.VAR_POSITIONAL for p in parameters))


if __name__ == "__main__":
    # [LAW:one-source-of-truth] Each command's signature is both its arity and its usage line.
    command, arguments = COMMANDS.get(sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if command is None or not _takes(command, len(arguments)):
        print("usage: " + " | ".join(_usage(name, fn) for name, fn in COMMANDS.items()), file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(command(*arguments))
    except Refused as error:
        print(error, file=sys.stderr)
        sys.exit(2)
    # Python exits 1 on an uncaught exception, which deliver uses for a prompt it withheld, so a crash
    # says so with 2 and the traceback.
    except Exception:
        traceback.print_exc()
        sys.exit(2)
