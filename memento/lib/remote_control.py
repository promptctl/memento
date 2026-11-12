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
      interrupts the asking turn, then posts /clear, each setting and the handoff, each once the log shows
      the session idle, and the goal once the handoff's turn has ended, unless a close-out in that turn
      claimed it; logging each step on stdout. Exits 1, saying what did not go through, when one did not.
  remote_control.py claim <session> <handoff dir>
      prints the goal an earlier handoff's worker published for the session, claimed for this close-out,
      or nothing when there is none.

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
import tempfile
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
# The most events a read back through the log asks for at once (the server's own ceiling).
HISTORY_PAGE = 200
# How many prompts an interrupt may name; one naming more is taken as naming none (2.1.299).
AIM = 8
# sysctl names (<sys/sysctl.h>): the size of the kernel's argument area, and a process's argc, exec path
# and argv in it.
CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49


class Refused(Exception):
    """The session cannot be reached or did not answer as the log promises; the message says why."""


class Transient(Refused):
    """No whole answer came, or one that may pass (an expired login, 429, 5xx): a later try may succeed,
    and a post may have landed."""


class Withheld(Exception):
    """A prompt did not go through, so nothing after it was posted: (what, why)."""


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


def interrupt_event(prompts):
    """An interrupt aimed at the turn whose prompts are `prompts`. The session stops its running turn only
    when the aim names one of that turn's prompts, and spares a turn whose prompts it knows and the aim
    does not name (2.1.299: `for_user_message_uuids`, at most AIM of them)."""
    return {"payload": {"type": "control_request", "uuid": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
                        "request": {"subtype": "interrupt", "for_user_message_uuids": list(prompts)}}}


def aim(payload):
    """The prompts a turn's stamped event names, as an interrupt aims at them, or None for any other
    event. The session stamps `user_message_uuid` and `user_message_uuids` on a turn's first top-level
    assistant event only (2.1.299). A turn begun by no client prompt, a task notification's, is not
    stamped, and keeps the previous turn's prompts as its own, so the newest stamp before it names it."""
    primary = payload.get("user_message_uuid")
    if payload.get("type") != "assistant" or payload.get("parent_tool_use_id") is not None \
            or not isinstance(primary, str):
        return None
    taken = [prompt for prompt in payload.get("user_message_uuids") or []
             if isinstance(prompt, str) and prompt != primary]
    return tuple([primary] + taken[-(AIM - 1):])


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
            raise Transient(f"the log took the event but returned no sequence number: {answer!r}") from None

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
        return self._events(f"/events?sort_order=asc&cursor={int(after)}")

    def history(self, before):
        """The first page of log events before sequence number `before`, newest first, and whether more
        precede them. A desc cursor reads strictly before it."""
        return self._events(f"/events?sort_order=desc&limit={HISTORY_PAGE}&cursor={int(before)}")

    def _events(self, path):
        page = self.request("GET", path)
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
    follows the last `user` event read. The log is read from a sequence number taken inside the asking
    turn, so the first `result` read ends it, and every stamp read before that is the asking turn's."""

    def __init__(self, session, after):
        self.session, self.cursor = session, after
        self.idle = False
        self.asking_ended = False
        self.asking_stamps = []  # what each stamp read before the asking turn's `result` names
        self.last_reset = 0  # sequence numbers start above 0
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
        seq, payload = parsed(self.session.id, event)
        if seq <= self.cursor:
            raise Refused(f"session {self.session.id}'s log gave event {seq} after {self.cursor}")
        kind, stamp = payload.get("type"), aim(payload)
        if kind == "user":
            self.idle = False
        elif kind == "result":
            self.idle = self.asking_ended = True
        elif kind == "conversation_reset":
            self.last_reset = seq
        if stamp and not self.asking_ended:
            self.asking_stamps.append(stamp)
        self.cursor = seq


def parsed(session_id, event):
    """An event's sequence number and payload. [LAW:parse-dont-validate] The one read of an event's shape."""
    try:
        seq, payload = int(event["sequence_num"]), event.get("payload") or {}
    except (KeyError, TypeError, ValueError, AttributeError):
        payload = None
    if not isinstance(payload, dict):
        raise Refused(f"session {session_id}'s log holds an event it cannot be read by: {event!r}"[:400])
    return seq, payload


def newest_stamp(session, head):
    """What the newest stamp at or before sequence number `head` names, or None when the current
    conversation holds none there. It is the asking turn's own, or the turn's before it, which an
    unstamped asking turn keeps as its own (`aim`); a stamp before the last conversation_reset names a
    conversation that is gone. A stamp later than `head` is left to the forward read."""
    before = head + 1
    while True:
        events, more = session.history(before)
        for event in events:
            seq, payload = parsed(session.id, event)
            if seq >= before:
                raise Refused(f"session {session.id}'s log gave event {seq} reading back from {before}")
            if stamp := aim(payload):
                return stamp
            if payload.get("type") == "conversation_reset":
                return None
            before = seq
        if not more:
            return None
        if not events:
            raise Refused(f"session {session.id}'s log promised more events before {before} but gave none")


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


def retried(call, timeout, note, doing, clock=time.monotonic, sleep=time.sleep):
    """What `call()` returns. A call that fails in passing is noted as `doing` and made again, and the last
    such failure is raised at the timeout; a refusal propagates."""
    failed = []

    def attempt():
        try:
            return [call()]
        except Transient as error:
            failed.append(error)
            note(f"{doing} failed, retrying: {error}")
            return None

    done = retry(attempt, timeout, clock, sleep)
    if done is None:
        raise failed[-1]
    return done[0]


def head(session, timeout, note, clock=time.monotonic, sleep=time.sleep):
    """The sequence number of the last event in the session's log (`retried`)."""
    return retried(session.head, timeout, note, "reading the log's head", clock, sleep)


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


def posted(session, event, what, timeout, note, clock=time.monotonic, sleep=time.sleep):
    """Posts `event`, which notes name as `what`, and returns its sequence number. One whose answer was
    lost is sent again as the same event until one is answered within `timeout`, and the server keeps one
    copy: it drops a repeated uuid, answering with the first copy's number (2.1.299). Every copy is sent
    before this returns, so before anything posted after it; one the server takes late is dropped only
    if it checks the uuid against the whole log, which was seen only back to back.
    [LAW:one-type-per-behavior] Prompts and interrupts are posted alike."""
    try:
        return retried(lambda: session.post(event), timeout, note, f"posting {what}", clock, sleep)
    except Transient as error:
        raise Refused(f"no answer to {what} within {timeout:g}s, though a copy may have landed: "
                      f"{error}") from None


def stopping_asking_turn(session, log, reset_timeout, note):
    """Stops the asking turn, the one whose Bash call ran the launcher: aims the first interrupt and
    returns what to wait on for the turn's `result`. Its newest stamp at or before the log's head is
    aimed at first, and every stamp the forward read meets before that `result` after it: no other turn
    starts before it, so each is the asking turn's. The interrupt is posted even when the turn has ended,
    since only the log can say so, and an aim naming a turn that has ended stops nothing the worker posts
    after it. A goal holds the turn working at every stop until its block cap, and an interrupt is the one
    way to end it from here (2.1.299). An interrupt that cannot be posted is an ERROR, and the wait goes
    on: refusing would leave the session with no handoff while the goal kept the old context working
    anyway."""
    aimed = []

    def aim_at(prompts):
        aimed.append(prompts)
        try:
            seq = posted(session, interrupt_event(prompts), "the interrupt", reset_timeout, note)
            note(f"posted an interrupt aimed at the asking turn's prompts {list(prompts)} (log sequence {seq})")
        except Refused as error:
            note(f"ERROR: the asking turn was not interrupted ({error}), so a goal can hold it working "
                 f"until its block cap before the /clear")

    try:
        stamp = retried(lambda: newest_stamp(session, log.cursor), reset_timeout, note,
                        "reading the log back for the asking turn's prompts")
    except (Refused, Transient) as error:
        stamp = None
        note(f"ERROR: the log could not be read back for the asking turn's prompts ({error}); it is "
             f"interrupted only if the forward read meets a stamp after the head")
    if stamp:
        aim_at(stamp)

    def ended(log):
        # A turn the log has seen end needs no aim at a stamp read in the same page.
        if not log.asking_ended:
            for stamp in log.asking_stamps:
                if stamp not in aimed:
                    aim_at(stamp)
        return log.asking_ended

    return ended


def published_goal(directory, session_id):
    """Where the carried goal is published for the close-out of the successor's first turn to claim."""
    return os.path.join(directory, f".remote-control-{session_id}.goal")


class Publication:
    """The carried goal, published under `path` while the handoff's first turn runs.
    [LAW:one-source-of-truth] One process owns it at a time: a claim renames the name away within the
    handoff directory and its publisher removes it, and of the two only one finds it there. The publisher
    holds an flock on it for its life, which is how a claim tells a live publisher from a dead one: a
    process's locks go when it does."""

    def __init__(self, path, held):
        self.path, self.held = path, held

    def take_back(self, note):
        """True when the goal is still this worker's, now taken back; False when a close-out claimed it.
        Removing the name has one winner against a claim's rename of it, as renames do. A name that cannot
        be removed is still this worker's goal, and its lock is let go, so a claim drops it as a dead
        publisher's."""
        try:
            os.remove(self.path)
        except FileNotFoundError:
            return False
        except OSError as error:
            self.held.close()
            note(f"WARNING: the published goal at {self.path} could not be removed ({error}); a claim drops it")
        return True


def publish(directory, session_id, goal):
    """Publishes the carried goal for the session, replacing one a dead publisher left."""
    path = published_goal(directory, session_id)
    descriptor, staged = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".")
    held = os.fdopen(descriptor, "w")
    held.write(goal)
    held.flush()
    fcntl.flock(held, fcntl.LOCK_EX)
    os.rename(staged, path)
    return Publication(path, held)


def claim(directory, session_id, note):
    """The goal published for the session, claimed by the close-out of the successor's first turn, or ""
    when there is none. Its publisher is alive when its lock is held, which is tested before the name is
    renamed onto the claim: a publisher that exits after the test had taken the goal back or lost it to
    this claim, and a file published between the two, which the claim then holds, is a live one. A goal
    whose publisher died is dropped, saying so, since a goal whose carrier died is not adopted days later."""
    path = published_goal(directory, session_id)
    mine = f"{path}.claim.{os.getpid()}"
    try:
        tested = open(path)
    except FileNotFoundError:
        return ""
    with tested:
        try:
            fcntl.flock(tested, fcntl.LOCK_EX | fcntl.LOCK_NB)
            live = False
        except BlockingIOError:
            live = True
        try:
            os.rename(path, mine)
        except FileNotFoundError:
            return ""
        live = live or os.stat(mine).st_ino != os.fstat(tested.fileno()).st_ino
    with open(mine) as handle:
        goal = handle.read()
    os.remove(mine)
    if live:
        return goal
    note(f"dropped the goal published for this session, since the worker that published it is gone: {goal}")
    return ""


def exclusive(directory, session_id, note):
    """Takes the lock on posting into the session, held until the process exits. It lives in `directory`,
    the handoff directory, which every worker of this user shares and no other user can write.
    [LAW:no-ambient-temporal-coupling] Two workers on one session, an earlier handoff's and the next
    close-out's, would otherwise post into each other's turns, and a prompt posted mid-turn is handed to
    the turn as text. So workers post one at a time, and each reads the session's state again once it
    holds the lock. Only the interrupt that ends a worker's own asking turn is posted before it, so that
    no earlier worker's waits keep the old context working."""
    handle = open(os.path.join(directory, f".remote-control-{session_id}.lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        note("an earlier handoff's worker is posting into this session; waiting for it to finish")
        fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def deliver(session, log, handoff, goal, settings, idle_timeout, reset_timeout, note, hold, directory):
    """Ends the asking turn, then posts /clear, each setting and the handoff, each once the log shows the
    session idle, and then the carried goal once the handoff's turn has ended, or raises Withheld naming
    the first prompt it could not post and where the carried goal went.

    The asking turn is interrupted and waited out before `hold()` takes the per-session lock
    (`stopping_asking_turn`, `exclusive`), and the session is read again once it is held. The /clear
    waits for the asking turn to end, and the handoff for the reset it brings. A command has finished when the log
    records its `result`, so the next is posted to an idle session. A wait whose length is the agent's
    work is bounded by `idle_timeout`, one for a command's effect by `reset_timeout`.

    [LAW:no-ambient-temporal-coupling] The goal goes in only once the handoff's turn has ended, since
    `/goal` starts a turn of its own and nothing sets a goal without one (2.1.299). It is published in
    `directory` before the handoff, so a close-out in that turn claims it (`claim`), and it is posted only
    if this worker takes it back: a goal turn started after a close-out would run in a context that has
    handed off."""
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
            seq = posted(session, user_event(session.id, text), what, reset_timeout, note)
        except Refused as error:
            raise Withheld(what, f"could not post it: {error}") from None
        note(f"posted {what} (log sequence {seq})")
        return seq

    def idle_after(seq):
        return lambda log: log.cursor >= seq and log.idle

    publication = None
    try:
        settle(stopping_asking_turn(session, log, reset_timeout, note), idle_timeout, "the /clear",
               "the asking turn did not end")
        hold()
        settle(idle_after(log.cursor), idle_timeout, "the /clear", "the session did not go idle")
        seq = send("/clear", "the /clear")
        settle(lambda log: log.last_reset > seq, reset_timeout, "the handoff", "the log recorded no conversation_reset")
        settle(idle_after(seq), reset_timeout, "the handoff", "the /clear did not finish")
        # The requested /model and /effort go in before the handoff, so its first turn already runs on them.
        for setting in settings:
            seq = send(setting, setting)
            settle(idle_after(seq), reset_timeout, "the handoff", f"{setting} did not finish")
        if goal:
            try:
                publication = publish(directory, session.id, goal)
            except OSError as error:
                raise Withheld("the handoff", f"the carried goal could not be published: {error}") from None
        seq = send(handoff, "the handoff")
        if not goal:
            return
        settle(idle_after(seq), idle_timeout, "the carried goal", "the handoff's turn did not end")
        if not publication.take_back(note):
            note(f"the carried goal {CLAIMED}")
            return
        publication = None
        send(f"/goal {goal}", "the carried goal")
    except Withheld as error:
        what, why = error.args
        raise Withheld(what, why + gone(goal, publication, note)) from None


CLAIMED = "went to the close-out that claimed it, which carries it on"


def gone(goal, publication, note):
    """Where the carried goal went when a delivery stopped short, taking it back first if it is still
    published: to the human the ERROR tells, or to a close-out that claimed it."""
    if not goal:
        return ""
    if publication is None or publication.take_back(note):
        return f". The carried goal was NOT set; deliver it by hand once the session is up: /goal {goal}"
    return f". The carried goal {CLAIMED}"


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
    goal, held = "", []
    try:
        try:
            after, idle, reset = int(after_seq), float(idle_timeout), float(reset_timeout)
        except ValueError as error:
            raise Refused(f"the worker was given a log head or timeout that is no number: {error}") from None
        # The goal is read before the handoff, so every ERROR below can say where it went.
        with open(goalfile) as handle:
            goal = handle.read()
        directory, session_id = os.path.dirname(os.path.abspath(msgfile)), session.rpartition("/")[2]
        with open(msgfile) as handle:
            handoff = handle.read()
        remote = Session(session, _login())
        deliver(remote, Log(remote, after), handoff, goal, setting, idle, reset, _note,
                lambda: held.append(exclusive(directory, session_id, _note)), directory)
    except Withheld as error:
        what, why = error.args
    # Before the first post: an argument that is no number, no handoff or goal to read, no login, or no lock.
    except (Refused, OSError) as error:
        what, why = "the /clear", str(error) + gone(goal, None, _note)
    else:
        _note("delivered")
        return 0
    _note(f"ERROR: {why}; {what} did NOT go through, so nothing after it was posted. "
          f"The handoff is preserved at {msgfile}")
    return 1


def _claim(session, directory):
    print(claim(directory, session.rpartition("/")[2], lambda line: print(line, file=sys.stderr)), end="")
    return 0


COMMANDS = {"session": _session, "head": _head, "deliver": _deliver, "claim": _claim}


if __name__ == "__main__":
    # -I keeps this file's directory off the path; lib/script.py is beside it.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import script
    script.run(COMMANDS, Refused)
