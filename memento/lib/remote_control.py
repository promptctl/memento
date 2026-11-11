#!/usr/bin/env python3
"""Posts prompts into a Claude Code Remote Control session through its event log on the session server.

A session that `claude remote-control` spawns is a `claude --print` child whose input is the server's
pipe, so nothing local can type into it. Its prompts arrive as client events in the session's log,
`/v1/code/sessions/<cse id>/events`, which is where claude.ai posts them; this posts them there the same
way, under the user's own claude.ai login. Run as a script:

  remote_control.py session <host pid> <claude pid>
      prints the URL of the Remote Control session a claude is, from its host's argv and its own, or
      nothing when its host is no `claude remote-control` server.
  remote_control.py head <session>
      prints the sequence number of the last event in the session's log, trying a read that fails in
      passing again for HEAD_TIMEOUT_SECONDS.
  remote_control.py deliver <session> <after seq> <msgfile> <goalfile> <idle timeout> <reset timeout> [<setting>...]
      posts /clear, each setting, the handoff and then the goal, each once the log shows the session idle,
      logging each step on stdout. Exits 1, saying what was not posted, when one could not be.

Each prints why it cannot answer on stderr and exits 2.

A prompt posted mid-turn can be delivered into the running turn at its next tool call, as text, so a
slash command posted then is read as words and never run (2.1.299). Post one only while the session is
idle, which a `result` after the last prompt marks.
"""

import ctypes
import fcntl
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
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
# claude goes, and the login is the user's whole claude.ai bearer, so it goes only over TLS or to this
# machine.
SESSION_URL = re.compile(r"(?:https://[^\s/]+|http://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?)"
                         r"/v1/code/sessions/cse_[A-Za-z0-9]+")
# The launcher reads the log's head while the agent waits on its Bash call, so a read that fails in passing
# is retried only this long before the handoff is refused.
HEAD_TIMEOUT_SECONDS = 15
# sysctl names (<sys/sysctl.h>): the size of the kernel's argument area, and a process's argc, exec path
# and argv in it.
CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49


class Refused(Exception):
    """The session cannot be reached or did not answer as the log promises; the message says why."""


class Transient(Refused):
    """No whole answer came, or one that may pass (an expired login, 429, 5xx): a later try may succeed,
    and a post may have landed."""


class Withheld(Exception):
    """A prompt was not posted, so nothing after it was either: (what, why)."""


def argv(pid):
    """The argument vector process `pid` runs with, as the kernel keeps it: one string per argument, so a
    prompt holding spaces is one token, where `ps -o args=` joins them all with spaces and a prompt reads
    like flags. [LAW:effects-at-boundaries] The one read of a process's arguments."""
    if sys.platform == "darwin":
        return _procargs(int(pid))
    with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
        tokens = handle.read().split(b"\0")
    # Each argument ends in a NUL, so the split leaves one empty string after the last.
    return [token.decode(errors="surrogateescape") for token in tokens[:-1]]


def _procargs(pid):
    """argv from macOS's KERN_PROCARGS2: argc, the exec path, NULs padding it, then argc strings."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]

    def sysctl(names, buffer, size):
        mib = (ctypes.c_int * len(names))(*names)
        length = ctypes.c_size_t(size)
        if libc.sysctl(mib, len(names), buffer, ctypes.byref(length), None, 0) != 0:
            number = ctypes.get_errno()
            raise OSError(number, f"cannot read process {pid}'s argv, sysctl {names}: {os.strerror(number)}")
        return length.value

    room = ctypes.c_int()
    sysctl((CTL_KERN, KERN_ARGMAX), ctypes.byref(room), ctypes.sizeof(room))
    area = ctypes.create_string_buffer(room.value)
    length = sysctl((CTL_KERN, KERN_PROCARGS2, pid), area, room.value)
    raw = area.raw[:length]
    argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
    # The padding is skipped as ps skips it, so an argv[0] that is empty is lost with it.
    tokens = raw[4:].partition(b"\0")[2].lstrip(b"\0").split(b"\0")
    if len(tokens) <= argc:
        raise OSError(f"process {pid}'s argument area holds fewer than its {argc} arguments")
    return [token.decode(errors="surrogateescape") for token in tokens[:argc]]


def arguments(argv):
    """A claude's arguments after its entry point: the binary, or node and the script it runs for an npm
    install."""
    return argv[2:] if argv and os.path.basename(argv[0]) == "node" else argv[1:]


def session_url(host_argv, claude_argv):
    """The URL of the Remote Control session a claude is, or None when its host is no `claude remote-control`
    server, which it is when its first argument is that subcommand: a prompt can only ever hold the word,
    and a server takes no prompt there, since it hands its child prompts on stdin.
    [LAW:parse-dont-validate] Every argument is matched as a whole token, so a prompt that quotes a flag
    is one token and never the flag."""
    if arguments(host_argv)[:1] != ["remote-control"]:
        return None
    following = claude_argv[1:] + [""]
    given = [following[i] if token == "--sdk-url" else token.partition("=")[2]
             for i, token in enumerate(claude_argv) if token == "--sdk-url" or token.startswith("--sdk-url=")]
    if len(given) != 1 or not SESSION_URL.fullmatch(given[0]):
        raise Refused(f"a `claude remote-control` server hosts this claude, but its argv gives "
                      f"{given!r} as --sdk-url, not one session URL: {claude_argv!r}"[:600])
    return given[0]


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


class _Unredirected(urllib.request.HTTPRedirectHandler):
    """Answers a redirect as the 3xx it is: urllib would send the login's bearer on to wherever it points."""

    def redirect_request(self, *args):
        return None


_OPENER = urllib.request.build_opener(_Unredirected)


class Session:
    """One session on the session server, reached under the login. [LAW:effects-at-boundaries] Every
    request goes through `request`."""

    def __init__(self, url, login):
        self.url, self.login = url, login
        self.id = url.rpartition("/")[2]

    def request(self, method, path, body=None):
        """One call, answered as JSON. A 401 is retried once under a re-read login, and is Transient if it
        comes again or the login cannot be read again, since Claude Code may be refreshing the stored login.
        Any other answer but 2xx is a refusal naming the status and the server's reason, and Transient when a
        retry may pass. A 2xx that is not JSON refuses a read, and is Transient for a post, which the server
        took.

        The platform is the one Claude Code's own CLI declares when it posts to a session. The session reads
        a prompt from a request that declares none as a message from an unknown peer, and runs no slash
        command in it: an idle session took a posted /clear as text (2.1.299)."""
        data = None if body is None else json.dumps(body).encode()
        where = f"{method} {self.url}{path}"
        for attempt in (1, 2):
            call = urllib.request.Request(self.url + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.login.token}", "Content-Type": "application/json",
                "anthropic-version": "2023-06-01", "anthropic-beta": "ccr-byoc-2025-07-29",
                "anthropic-client-platform": "claude_code_cli"})
            try:
                with _OPENER.open(call, timeout=30) as answer:
                    text = answer.read()
                break
            except urllib.error.HTTPError as error:
                status, reason = error.code, _body(error)
            # A refused connection, a timeout, a reset, a body cut short: no whole answer.
            except (OSError, http.client.HTTPException) as error:
                raise Transient(f"{where} got no answer: {error!r}") from None
            if status == 401 and attempt == 1:
                try:
                    self.login.refresh()
                except Refused as error:
                    raise Transient(f"{where} answered HTTP 401, and the login could not be read again: "
                                    f"{error}") from None
                continue
            passing = status in (401, 429) or status >= 500
            raise (Transient if passing else Refused)(f"{where} answered HTTP {status}: {reason}")
        try:
            return json.loads(text)
        except ValueError:
            unread = Refused if body is None else Transient
            raise unread(f"{where} answered with no JSON: {text[:400]!r}") from None

    def post(self, event):
        """Appends an event to the log and returns the sequence number it was given. Transient when the
        server took it without saying where."""
        answer = self.request("POST", "/events", {"events": [event]})
        try:
            return int(answer["results"][0]["sequence_num"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise Transient(f"the log took the prompt but returned no sequence number: {answer!r}") from None

    def head(self):
        """The sequence number of the last event in the log."""
        page = self.request("GET", "/events?sort_order=desc&limit=1")
        try:
            return int(page["data"][0]["sequence_num"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise Refused(f"session {self.id}'s log answered with no last event: {page!r}") from None

    def page(self, after):
        """The first page of log events after sequence number `after`, oldest first, and whether more
        follow it."""
        page = self.request("GET", f"/events?sort_order=asc&cursor={int(after)}")
        if not isinstance(page, dict) or not isinstance(page.get("data", []), list):
            raise Refused(f"session {self.id}'s log answered with no page of events: {page!r}"[:400])
        return page.get("data", []), bool(page.get("next_cursor"))


def _body(error):
    try:
        return error.read().decode(errors="replace")[:400]
    except (OSError, http.client.HTTPException) as unread:
        return f"(its body could not be read: {unread!r})"


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
        self.unreachable = None  # why the last read got no answer, or None when it got one

    def read(self):
        """Reads every page after the cursor, taking each in as it comes. The cursor is the last event's
        sequence number, so each page moves it on, and a page that does not is a log that cannot be read
        to its end."""
        while True:
            events, more = self.session.page(self.cursor)
            start = self.cursor
            for event in events:
                self._take(event)
            if not more:
                return
            if self.cursor == start:
                raise Refused(f"session {self.session.id}'s log promised more events after {start} "
                              f"but gave none")

    def _take(self, event):
        try:
            seq, payload = int(event["sequence_num"]), event.get("payload") or {}
            kind = payload.get("type")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise Refused(f"session {self.session.id}'s log holds an event it cannot be read by: "
                          f"{event!r}"[:400]) from None
        if seq <= self.cursor:
            raise Refused(f"session {self.session.id}'s log gave event {seq} after {self.cursor}")
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


def head(session, timeout, note, clock=time.monotonic, sleep=time.sleep):
    """The sequence number of the last event in the session's log. A read that fails in passing is noted and
    tried again, and the last such failure is raised at the timeout; a refusal propagates."""
    failed = []

    def attempt():
        try:
            return session.head()
        except Transient as error:
            failed.append(error)
            note(f"reading the log's head failed, retrying: {error}")
            return None

    seq = retry(attempt, timeout, clock, sleep)
    if seq is None:
        raise failed[-1]
    return seq


def read(log, note):
    """Reads the log on. A read that failed in passing is noted, and kept as why the log is unreachable
    until one passes; a refusal propagates."""
    try:
        log.read()
        log.unreachable = None
    except Transient as error:
        log.unreachable = error
        note(f"reading the log failed, retrying: {error}")


def await_state(log, settled, timeout, note, clock=time.monotonic, sleep=time.sleep):
    """Reads the log until `settled(log)`: True once it is, False at the timeout.
    [LAW:no-ambient-temporal-coupling] The session's own log is the one signal of where it stands."""
    return retry(lambda: read(log, note) or settled(log) or None, timeout, clock, sleep) is not None


def post(session, log, text, timeout, note):
    """Posts a prompt once and returns its sequence number. A post whose answer was lost may still have
    landed, and a second /clear behind a handoff would wipe it, so a prompt is never sent again: the log
    settles it by the prompt's uuid within `timeout`, and a prompt the log never shows is a refusal."""
    event = user_event(session.id, text)
    try:
        return session.post(event)
    except Transient as error:
        note(f"posting got no clear answer, reading the log for the prompt: {error}")
        unanswered = error
    posted = event["payload"]["uuid"]
    if await_state(log, lambda log: posted in log.prompts, timeout, note):
        return log.prompts[posted]
    raise Refused(f"posting got no clear answer ({unanswered}), and the log did not show the prompt "
                  f"within {timeout:g}s")


def exclusive(directory, session_id, note):
    """Takes the lock on posting into the session, held until the process exits. It lives in `directory`,
    the handoff directory, which every worker of this user shares and no other user can write.
    [LAW:no-ambient-temporal-coupling] An earlier handoff's worker waiting to post its carried goal and the
    next agent's worker waiting to post /clear wait on the same `result`, and whichever posts second is
    handed to the first one's turn as text. So workers on one session post one at a time, and each reads
    the session's state only once it holds the lock."""
    handle = open(os.path.join(directory, f".remote-control-{session_id}.lock"), "a")
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
        unreachable = f" (the log was unreachable: {log.unreachable})" if log.unreachable else ""
        raise Withheld(what, f"{why} within {timeout:g}s{unreachable}")

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


def _session(host_pid, claude_pid):
    print(session_url(argv(host_pid), argv(claude_pid)) or "")
    return 0


def _head(session):
    print(head(Session(session, _login()), HEAD_TIMEOUT_SECONDS, lambda line: print(line, file=sys.stderr)))
    return 0


def _deliver(session, after_seq, msgfile, goalfile, idle_timeout, reset_timeout, *setting):
    _note(f"waking; remote-control session {session}, log head {after_seq}")
    try:
        try:
            after, idle, reset = int(after_seq), float(idle_timeout), float(reset_timeout)
        except ValueError as error:
            raise Refused(f"the worker was given a log head or timeout that is no number: {error}") from None
        lock = exclusive(os.path.dirname(os.path.abspath(msgfile)), session.rpartition("/")[2], _note)  # noqa: F841 - held until exit
        with open(msgfile) as handle:
            handoff = handle.read()
        with open(goalfile) as handle:
            goal = handle.read()
        remote = Session(session, _login())
        deliver(remote, Log(remote, after), handoff, goal, setting, idle, reset, _note)
    except Withheld as error:
        what, why = error.args
    # Before the first post: an argument that is no number, no lock, no handoff or goal to read, or no login.
    except (Refused, OSError) as error:
        what, why = "the /clear", str(error)
    else:
        _note("delivered")
        return 0
    _note(f"ERROR: {why}; {what} was NOT posted, nor anything after it. The handoff is preserved at {msgfile}")
    return 1


COMMANDS = {"session": _session, "head": _head, "deliver": _deliver}


if __name__ == "__main__":
    # -I keeps this file's directory off the path; lib/script.py is beside it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import script
    script.run(COMMANDS, Refused)
