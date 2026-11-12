#!/usr/bin/env python3
"""Tests for the Remote Control poster: which session a claude's argv names, where Claude Code keeps
the login, what the log shows of the session, the order and timing of a handoff's posts, and the calls to
the session server. The handoff runs in-process against fake session logs, the calls against the same logs
served on localhost (fake_session_server.py), and the script form once as the launcher runs it.

Run with: python3 remote_control.test.py
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import hermetic  # noqa: E402

hermetic.home()
import fake_session_server  # noqa: E402
import remote_control  # noqa: E402
from fake_session_server import SessionLog  # noqa: E402
from remote_control import Log, Refused, Transient, Withheld  # noqa: E402

failures = []
SESSION = "cse_01N5sakbg7CFUBuSSUMs5Le1"


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def raised(call):
    """The exception `call` raised, or None when it returned."""
    try:
        call()
    except Exception as error:
        return error
    return None


def wait_for(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    return condition()


# --- the session a claude's argv names ----------------------------------------
# The vectors `claude remote-control` and the claude it spawns run with (2.1.299, read with ps on a live
# pair): the session's URL as --sdk-url and again as --resume=, and its id as --session-id.
URL = f"https://api.anthropic.com/v1/code/sessions/{SESSION}"
SPAWNED = ["/Users/bmf/.local/share/claude/versions/2.1.299", "--print", "--sdk-url", URL, "--session-id", SESSION,
           "--input-format", "stream-json", "--output-format", "stream-json", "--replay-user-messages",
           f"--resume={URL}", f"--mcp-config=/Users/bmf/.claude/bridge-spawn/{SESSION}-doIX9g/mcp.json",
           "--model", "claude-opus-5-5"]
SERVER = ["/Users/bmf/.local/bin/claude", "remote-control", "--spawn", "worktree", "--name", "memento"]
DAEMON = ["/Users/bmf/.local/share/claude/versions/2.1.299", "daemon", "run", "--bg-pty-host"]

check("a claude a remote-control server spawned is the session its --sdk-url names",
      remote_control.session_url(SERVER, SPAWNED) == URL)
check("as is one a node install's server spawned, whose first argument is its script",
      remote_control.session_url(["node", "/opt/homebrew/bin/claude", "remote-control"], SPAWNED) == URL)
check("the --sdk-url=<url> spelling names the session too",
      remote_control.session_url(SERVER, ["claude", "--print", f"--sdk-url={URL}"]) == URL)
# A claude's prompt is one argument however it reads, and a server's first argument is never one: a
# claude started with a prompt about this transport, by hand or from a tool call, hosts no session of its
# own, and a relaunched claude's prompt is a handoff that may quote a spawned claude's argv.
QUOTING = f"fix the remote-control transport: claude --print --sdk-url {URL}"
for name, host in (("a daemon host", DAEMON), ("a claude whose prompt holds the word", ["claude", QUOTING]),
                   ("a claude whose later argument is the word", ["claude", "--resume", "remote-control"]),
                   ("a node claude whose script is named for it", ["node", "remote-control", "--help"]),
                   ("a host with no arguments", ["claude"]), ("an empty argv", [])):
    check(f"a claude hosted by {name} is no Remote Control session, whatever its own argv gives",
          remote_control.session_url(host, SPAWNED) is None)
for name, url in (("over plain http to another machine", f"http://api.anthropic.com/v1/code/sessions/{SESSION}"),
                  ("over http to a host that only starts with loopback's name",
                   f"http://localhost.example.com/v1/code/sessions/{SESSION}")):
    check(f"a --sdk-url {name} is refused, since the login would go there in the clear",
          type(raised(lambda: remote_control.session_url(SERVER, ["claude", "--sdk-url", url]))) is Refused)
LOOPBACK = f"http://127.0.0.1:8080/v1/code/sessions/{SESSION}"
check("one over http to this machine names the session",
      remote_control.session_url(SERVER, ["claude", "--sdk-url", LOOPBACK]) == LOOPBACK)
for name, url in (("over plain http to another machine", f"http://api.anthropic.com/v1/code/sessions/{SESSION}"),
                  ("over http to a host that only starts with loopback's name",
                   f"http://localhost.example.com/v1/code/sessions/{SESSION}")):
    check(f"a --sdk-url {name} is refused, since the login would go there in the clear",
          type(raised(lambda: remote_control.session_url(SERVER, ["claude", "--sdk-url", url]))) is Refused)
LOOPBACK = f"http://127.0.0.1:8080/v1/code/sessions/{SESSION}"
check("one over http to this machine names the session",
      remote_control.session_url(SERVER, ["claude", "--sdk-url", LOOPBACK]) == LOOPBACK)
for name, claude, given in (("no --sdk-url", ["claude", "--print"], "[]"),
                            ("a prompt that quotes one", ["claude", "--print", QUOTING], "[]"),
                            ("a --sdk-url with no value", ["claude", "--print", "--sdk-url"], "['']"),
                            ("a value that is no session URL", ["claude", "--sdk-url", "https://example.com/x"],
                             "['https://example.com/x']"),
                            ("a URL with more after it", ["claude", f"--sdk-url={URL}/events"], "/events']"),
                            ("two", SPAWNED + ["--sdk-url", URL[:-1] + "x"], "Lex'")):
    error = raised(lambda: remote_control.session_url(SERVER, claude))
    check(f"a remote-control server's claude whose argv gives {name} is refused, since nothing else reaches it",
          type(error) is Refused and f"gives {given}" in str(error) if given.startswith("[")
          else type(error) is Refused and given in str(error), error)


def started(*arguments, cwd=None):
    """A live process running with exactly `arguments` after its interpreter, which sleeps until killed."""
    process = subprocess.Popen([sys.executable, *arguments], cwd=cwd,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for(lambda: remote_control.argv(process.pid)[1:2] == [arguments[0]])
    return process


SLEEPS = "import time; time.sleep(60)"
# The kernel's own argv, not ps's text: a token holding spaces, one that quotes a flag, an empty one.
TOKENS = ["-c", SLEEPS, "fix the remote-control bug", f"--sdk-url {URL}", "", "--sdk-url", URL]
process = started(*TOKENS)
try:
    read = remote_control.argv(process.pid)
    check("a process's argv is read as the vector it was started with", read[1:] == TOKENS, read)
finally:
    process.kill()
error = raised(lambda: remote_control.argv(2 ** 22 + 7))
check("a process that is not there is an OSError, which the script reports as a refusal",
      isinstance(error, OSError), error)

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
EMPTY_SECURE = {"CLAUDE_CONFIG_DIR": "/elsewhere", "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""}
check("an empty secure-storage dir means the default login, whatever the config dir, on and off macOS",
      remote_control.credential_service(EMPTY_SECURE) == "Claude Code-credentials"
      and remote_control.credential_file(EMPTY_SECURE)
      == os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json"))
check("off macOS the login is .credentials.json in the config dir",
      remote_control.credential_file({"CLAUDE_CONFIG_DIR": "/cfg"}) == "/cfg/.credentials.json")
check("the access token is read from the stored login",
      remote_control.access_token('{"claudeAiOauth": {"accessToken": "tok"}}') == "tok")
check("a stored login without an access token is refused",
      isinstance(raised(lambda: remote_control.access_token('{"mcpOAuth": {}}')), Refused))

event = remote_control.user_event(SESSION, "/clear")["payload"]
check("a prompt is a user event naming the session in its session_ spelling",
      event["type"] == "user" and event["session_id"] == "session_01N5sakbg7CFUBuSSUMs5Le1"
      and event["message"] == {"role": "user", "content": "/clear"}, event)
check("every prompt carries its own uuid",
      event["uuid"] != remote_control.user_event(SESSION, "/clear")["payload"]["uuid"])

# --- what the log shows --------------------------------------------------------


def fresh(**options):
    """A session log of its own, and the head read from inside its asking turn."""
    log = SessionLog(f"cse_{uuid.uuid4().hex}", **options)
    return log, log.head()


session, head = fresh()
log = Log(session, head)
log.read()
check("read from inside the asking turn, the session is busy", not log.idle)
session.end_turn()
log.read()
check("and idle once the log holds the turn's result", log.idle)
session.post(remote_control.user_event(session.id, "/clear"))
log.read()
check("a /clear's reset is read with its sequence number", log.last_reset > head and log.idle, vars(log))


class Pages:
    """A session whose log answers with the given pages, one per read, as (events, whether more follow)."""
    id = "cse_pages"

    def __init__(self, *pages):
        self.pages = iter(pages)

    def page(self, after):
        return next(self.pages)


for name, pages in (("an event with no sequence number", ([{"payload": {"type": "assistant"}}], False)),
                    ("an event whose payload is no object", ([{"sequence_num": "12", "payload": "text"}], False)),
                    ("an event older than the cursor", ([{"sequence_num": "3", "payload": {}}], False)),
                    ("a page promising more after no events", ([], True))):
    error = raised(Log(Pages(pages), 10).read)
    check(f"{name} is a refusal, not a crash or an endless read", type(error) is Refused, error)


class Recording:
    """A session log that records where every page read starts, and fails the reads it is told to."""
    def __init__(self, log, failing=()):
        self.log, self.id, self.cursors, self.failing = log, log.id, [], list(failing)

    def page(self, after):
        self.cursors.append(after)
        if len(self.cursors) in self.failing:
            raise Transient("GET answered HTTP 502: bad gateway")
        return self.log.page(after)

    def history(self, before):
        return self.log.history(before)

    def post(self, event):
        return self.log.post(event)


session, head = fresh()
recording = Recording(session)
log = Log(recording, head)
log.read()
session.end_turn()
log.read()
log.read()
check("each read asks only for events after the last one read",
      recording.cursors == [head, head, session.head()], recording.cursors)
for _ in range(2):
    session.post(remote_control.user_event(session.id, "/clear"))
recording = Recording(session, failing=(2,))
log = Log(recording, head)
error = raised(log.read)
taken = log.cursor
log.read()
check("a read takes in every page, and one that fails part-way keeps the pages it took in",
      isinstance(error, Transient) and taken == head + fake_session_server.PAGE
      and log.cursor == session.head() and log.idle and recording.cursors[2] == taken,
      (error, taken, recording.cursors))


class Clock:
    """Time that moves only when the code under test sleeps."""
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


session, head = fresh()
session.end_turn()
notes, clock = [], Clock()
log = Log(Recording(session, failing=(1, 2)), head)
check("a wait reads through reads that got no answer, noting each",
      remote_control.await_state(log, lambda log: log.idle, 60, notes.append, clock, clock.sleep)
      and len(notes) == 2 and "HTTP 502" in notes[0], notes)
session, head = fresh()
slept, clock = [], Clock()
check("a wait for what never comes ends at its timeout",
      not remote_control.await_state(Log(session, head), lambda log: log.idle, 100, notes.append, clock,
                                     lambda seconds: slept.append(seconds) or clock.sleep(seconds))
      and 100 <= clock.now < 100 + remote_control.POLL_CEILING_SECONDS, clock.now)
check("and its polling backs off, doubling up to a ceiling of seconds",
      slept[:2] == [remote_control.POLL_SECONDS, 2 * remote_control.POLL_SECONDS]
      and max(slept) == remote_control.POLL_CEILING_SECONDS <= 5, slept[:12])


class Hiccups:
    """A session whose head reads fail in passing `failing` times, then give 42."""
    id = "cse_hiccups"

    def __init__(self, failing):
        self.failing = failing

    def head(self):
        if self.failing:
            self.failing -= 1
            raise Transient("GET answered HTTP 502: bad gateway")
        return 42


notes, clock = [], Clock()
check("the head is read again after a read that failed in passing, noting it",
      remote_control.head(Hiccups(2), 15, notes.append, clock, clock.sleep) == 42
      and len(notes) == 2 and "HTTP 502" in notes[0], notes)
clock = Clock()
error = raised(lambda: remote_control.head(Hiccups(1000), 15, notes.append, clock, clock.sleep))
check("and one that keeps failing raises its last failure at the timeout",
      isinstance(error, Transient) and 15 <= clock.now < 15 + remote_control.POLL_CEILING_SECONDS,
      (error, clock.now))



class Hiccups:
    """A session whose head reads fail in passing `failing` times, then give `seq`."""
    id = "cse_hiccups"

    def __init__(self, failing, seq=42):
        self.failing, self.seq = failing, seq

    def head(self):
        if self.failing:
            self.failing -= 1
            raise Transient("GET answered HTTP 502: bad gateway")
        return self.seq


notes, clock = [], Clock()
check("the head is read again after a read that failed in passing, noting it",
      remote_control.head(Hiccups(2), 15, notes.append, clock, clock.sleep) == 42
      and len(notes) == 2 and "HTTP 502" in notes[0], notes)
clock = Clock()
error = raised(lambda: remote_control.head(Hiccups(1000), 15, notes.append, clock, clock.sleep))
check("and one that keeps failing raises its last failure at the timeout",
      isinstance(error, Transient) and 15 <= clock.now < 15 + remote_control.POLL_CEILING_SECONDS,
      (error, clock.now))

# --- a handoff's posts -----------------------------------------------------------

HANDOFFS = tempfile.mkdtemp(prefix="remote-control-handoffs.")


def run_deliver(session, head, goal="", settings=(), idle_timeout=10, reset_timeout=2):
    """Delivers into the session and returns (what was withheld and why, or None; the notes)."""
    notes = []
    error = raised(lambda: remote_control.deliver(session, Log(session, head), "the handoff", goal, settings,
                                                  idle_timeout, reset_timeout, notes.append, lambda: None, HANDOFFS))
    return (error.args if isinstance(error, Withheld) else error), notes


def claimed(session):
    return remote_control.claim(HANDOFFS, session.id, lambda line: None)


session, head = fresh()
session.end_turn()
withheld, notes = run_deliver(session, head, goal="the tests pass", settings=("/effort high",))
check("a handoff posts /clear, each setting, the handoff, and the goal, in that order",
      withheld is None and session.prompts() == ["/clear", "/effort high", "the handoff", "/goal the tests pass"],
      (withheld, session.posts))
check("each to an idle session", all(idle for _, idle, _ in session.posts), session.posts)
check("and notes each post with its sequence number",
      any(note.startswith("posted the /clear (log sequence ") for note in notes), notes)
check("an asking turn that has already ended is still interrupted first, which stops nothing",
      session.interrupts == [(("asking-turn",), None)], session.interrupts)
check("and the goal it published is taken back, so no close-out can claim it after", claimed(session) == "")

# The goal's Stop hook holds the turn that ran the close-out working at every stop, so it would go on
# working in the old context until its block cap; an interrupt is the one way to end it from here.
session, head = fresh(goal="the old goal")
withheld, notes = run_deliver(session, head, goal="the tests pass")
check("an asking turn a goal holds is ended by an interrupt aimed at its prompt, before the /clear",
      withheld is None and session.interrupts == [(("asking-turn",), ("asking-turn",))]
      and session.prompts() == ["/clear", "the handoff", "/goal the tests pass"],
      (withheld, session.interrupts, notes))
check("and the handoff's turn runs with no goal, the /clear having ended the old one",
      session.turns[0] == ("the handoff", None), session.turns)

session, _ = fresh(goal="the old goal")
session.append(*[{"type": "assistant", "parent_tool_use_id": None}] * (3 * fake_session_server.PAGE))
withheld, _ = run_deliver(session, session.head())
check("its stamp is found reading back across several pages of the turn's later events",
      withheld is None and session.interrupts == [(("asking-turn",), ("asking-turn",))], session.interrupts)

session, head = fresh(stamped=False)
withheld, _ = run_deliver(session, head)
check("an asking turn no client prompt began is aimed at by the stamp before it, which it keeps as its own",
      withheld is None and session.interrupts == [(("previous-turn",), ("previous-turn",))], session.interrupts)

session, head = fresh(late=True)
session.upload_stamp()
withheld, _ = run_deliver(session, head)
check("an asking turn whose stamp is uploaded after the head is aimed at once the forward read meets it",
      withheld is None and session.interrupts == [(("asking-turn",), ("asking-turn",))], session.interrupts)

prompts = tuple(f"p{n}" for n in range(12))
check("an aim names the turn's own prompt and the newest it took in, at most AIM of them",
      remote_control.aim(SessionLog.stamp(prompts)) == ("p0",) + prompts[-(remote_control.AIM - 1):])
check("and a stamp is only a turn's top-level assistant event",
      remote_control.aim({**SessionLog.stamp(prompts), "parent_tool_use_id": "toolu_1"}) is None
      and remote_control.aim({"type": "user", "user_message_uuid": "p0"}) is None)


class Interrupts:
    """A session whose interrupts get the answer `answer(event)` gives: a sequence number, or it raises."""
    def __init__(self, log, answer):
        self.log, self.id, self.answer, self.sent = log, log.id, answer, []

    def page(self, after):
        return self.log.page(after)

    def history(self, before):
        return self.log.history(before)

    def post(self, event):
        if event["payload"]["type"] != "control_request":
            return self.log.post(event)
        self.sent.append(event["payload"]["uuid"])
        return self.answer(event)


def refused(event):
    raise Refused("POST answered HTTP 403: fixture refuses")


session, head = fresh()
session.end_turn()
withheld, notes = run_deliver(Interrupts(session, refused), head)
check("an interrupt that cannot be posted is an ERROR, and the handoff still goes in once the turn ends",
      withheld is None and session.prompts() == ["/clear", "the handoff"]
      and any(note.startswith("ERROR: the asking turn was not interrupted") for note in notes), notes)

session, head = fresh(goal="the old goal")
lost = []


def lost_once(event):
    seq = session.post(event)
    if not lost:
        lost.append(event)
        raise Transient("POST got no answer: timed out")
    return seq


interrupts = Interrupts(session, lost_once)
withheld, notes = run_deliver(interrupts, head)
check("an interrupt whose answer was lost is sent again as the same event, which the log holds once",
      withheld is None and len(interrupts.sent) == 2 and len(set(interrupts.sent)) == 1
      and session.interrupts == [(("asking-turn",), ("asking-turn",))], (interrupts.sent, session.interrupts))

session, head = fresh(deaf=True)
withheld, _ = run_deliver(session, head, goal="the tests pass", idle_timeout=1)
check("an asking turn the interrupt does not stop gets no /clear, which it would read as text",
      session.prompts() == [] and withheld[0] == "the /clear" and "did not end" in withheld[1], withheld)
check("and the goal it carried is named to set by hand", "/goal the tests pass" in withheld[1], withheld)

session, head = fresh(resets=False)
session.end_turn()
withheld, _ = run_deliver(session, head, goal="the tests pass", reset_timeout=1)
check("a reset the log never records holds the handoff back, so it never lands in the old conversation",
      session.prompts() == ["/clear"] and withheld[0] == "the handoff" and "conversation_reset" in withheld[1],
      withheld)
check("and the goal it carried is given to the human to set by hand",
      "NOT set" in withheld[1] and "/goal the tests pass" in withheld[1], withheld)

session, head = fresh(holds=True)
session.end_turn()
thread = threading.Thread(target=run_deliver, args=(session, head, "the tests pass"))
thread.start()
posted_handoff = wait_for(lambda: "the handoff" in session.prompts())
time.sleep(1)
check("the goal waits for the handoff's turn to end, since /goal starts a turn of its own",
      posted_handoff and session.prompts() == ["/clear", "the handoff"], session.prompts())
session.end_turn()
thread.join(10)
check("and is posted once it has", session.prompts()[-1] == "/goal the tests pass" and session.posts[-1][1],
      session.posts)


class RefusesGoal:
    """A session that refuses the /goal prompt."""
    def __init__(self, log):
        self.log, self.id = log, log.id

    def __getattr__(self, name):
        return getattr(self.log, name)

    def post(self, event):
        if str(event["payload"].get("message", {}).get("content", "")).startswith("/goal"):
            raise Refused("POST answered HTTP 403: forbidden")
        return self.log.post(event)


session, head = fresh()
session.end_turn()
withheld, _ = run_deliver(RefusesGoal(session), head, goal="the tests pass")
check("a goal taken back whose post is refused is given to the human to set by hand",
      withheld[0] == "the carried goal" and "HTTP 403" in withheld[1] and "/goal the tests pass" in withheld[1]
      and claimed(session) == "", withheld)

session, _ = fresh()
session.append({"type": "conversation_reset"}, {"type": "assistant", "parent_tool_use_id": None})
check("a stamp from before the last conversation_reset is not read back as the asking turn's",
      remote_control.newest_stamp(session, session.head()) is None)

class Unanswered:
    """A session whose first `failing` prompts get no clear answer; `lands` says whether each still
    reaches the log. It records the uuid of every prompt it is sent."""
    def __init__(self, log, lands, failing=1000):
        self.log, self.id, self.lands, self.failing, self.sent = log, log.id, lands, failing, []

    def page(self, after):
        return self.log.page(after)

    def history(self, before):
        return self.log.history(before)

    def post(self, event):
        if event["payload"]["type"] != "user":
            return self.log.post(event)
        self.sent.append(event["payload"]["uuid"])
        if not self.failing:
            return self.log.post(event)
        self.failing -= 1
        if self.lands:
            self.log.post(event)
        raise Transient("POST got no answer: timed out")


# A /clear the log took twice could land behind the handoff and wipe it, so a re-sent post must be the
# same event, which the log keeps once.
session, head = fresh()
session.end_turn()
unanswered = Unanswered(session, lands=True, failing=1)
withheld, notes = run_deliver(unanswered, head)
check("a post whose answer was lost but which landed is sent again as the same event, which the log holds once",
      withheld is None and session.prompts() == ["/clear", "the handoff"]
      and len(unanswered.sent) == 3 and unanswered.sent[0] == unanswered.sent[1],
      (withheld, notes, unanswered.sent))
session, head = fresh()
session.end_turn()
unanswered = Unanswered(session, lands=False, failing=1)
withheld, notes = run_deliver(unanswered, head)
check("one whose first copy did not land is sent again as the same event, and lands once",
      withheld is None and session.prompts() == ["/clear", "the handoff"]
      and len(unanswered.sent) == 3 and unanswered.sent[0] == unanswered.sent[1], (withheld, notes, unanswered.sent))
session, head = fresh()
session.end_turn()
unanswered = Unanswered(session, lands=True)
withheld, notes = run_deliver(unanswered, head, reset_timeout=1)
check("one never answered within the timeout is withheld saying a copy may have landed, with the last failure",
      session.prompts() == ["/clear"] and len(set(unanswered.sent)) == 1 and len(unanswered.sent) > 1
      and withheld[0] == "the /clear" and "no answer to the /clear within 1s, though a copy may have landed" in withheld[1]
      and "timed out" in withheld[1], (withheld, unanswered.sent))
session, head = fresh(deaf=True)
withheld, _ = run_deliver(Recording(session, failing=range(1, 1000)), head, idle_timeout=1)
check("a wait the log never answered says so, not only that the session did not change",
      withheld[0] == "the /clear" and "did not end" in withheld[1] and "unreachable" in withheld[1]
      and "HTTP 502" in withheld[1], withheld)

# The handoff's first turn may close out in turn, before the goal is posted. The goal then belongs to the
# successor that close-out starts; posted after it, it would start a turn in the context that handed off.
session, head = fresh(holds=True)
session.end_turn()
result = []
thread = threading.Thread(target=lambda: result.append(run_deliver(session, head, "the tests pass")))
thread.start()
wait_for(lambda: "the handoff" in session.prompts())
check("a close-out in the handoff's turn claims the goal its worker published", claimed(session) == "the tests pass")
check("which is then no longer there to claim", claimed(session) == "")
session.end_turn()
thread.join(10)
withheld, notes = result[0]
check("and the worker posts no goal into the context that handed off, saying where it went",
      withheld is None and session.prompts() == ["/clear", "the handoff"]
      and any("went to the close-out" in note for note in notes), (withheld, notes))

session, head = fresh(holds=True)
session.end_turn()
withheld, _ = run_deliver(session, head, goal="the tests pass", idle_timeout=1)
check("a handoff's turn that never ends withholds the goal, giving it to the human to set by hand",
      withheld[0] == "the carried goal" and "/goal the tests pass" in withheld[1], withheld)
check("and takes it back, so a close-out run later claims nothing", claimed(session) == "")

session, head = fresh(holds=True)
session.end_turn()
result = []
thread = threading.Thread(target=lambda: result.append(run_deliver(session, head, "the tests pass", idle_timeout=2)))
thread.start()
wait_for(lambda: "the handoff" in session.prompts())
claimed(session)
thread.join(10)
withheld, _ = result[0]
check("but one a close-out claimed first is said to have gone to it, not handed to the human as well",
      withheld[0] == "the carried goal" and "went to the close-out" in withheld[1] and "/goal" not in withheld[1],
      withheld)

notes = []
publication = remote_control.publish(HANDOFFS, "cse_dead01", "the old goal")
publication.held.close()
check("a goal whose publisher is gone is dropped by its claim, saying so",
      remote_control.claim(HANDOFFS, "cse_dead01", notes.append) == "" and "is gone" in notes[0]
      and not os.listdir(HANDOFFS), (notes, os.listdir(HANDOFFS)))
publication, rename = remote_control.publish(HANDOFFS, "cse_exits", "the tests pass"), os.rename


def exiting_first(source, target):
    """The publisher exits between the claim's test of its lock and the claim's rename."""
    publication.held.close()
    rename(source, target)


os.rename = exiting_first
try:
    taken = remote_control.claim(HANDOFFS, "cse_exits", notes.append)
finally:
    os.rename = rename
check("a goal whose publisher exits after the claim tested its lock is still claimed",
      taken == "the tests pass" and not os.listdir(HANDOFFS), (taken, os.listdir(HANDOFFS)))
publication, stuck = remote_control.publish(HANDOFFS, "cse_stuck", "the tests pass"), []
os.chmod(HANDOFFS, 0o500)
try:
    kept = publication.take_back(stuck.append)
finally:
    os.chmod(HANDOFFS, 0o700)
check("a published goal that cannot be removed is still the worker's, and a claim drops it as a dead publisher's",
      kept and "could not be removed" in stuck[0]
      and remote_control.claim(HANDOFFS, "cse_stuck", stuck.append) == "" and not os.listdir(HANDOFFS), stuck)
remote_control.publish(HANDOFFS, "cse_dead02", "the old goal").held.close()
publication = remote_control.publish(HANDOFFS, "cse_dead02", "the new goal")
check("and one a live publisher replaced is claimed as live",
      remote_control.claim(HANDOFFS, "cse_dead02", notes.append) == "the new goal"
      and not publication.take_back(lambda line: None) and not os.listdir(HANDOFFS), os.listdir(HANDOFFS))

# Two workers on one session: an earlier handoff's, waiting to post its carried goal once the handoff's
# turn ends, and the next one's, from a close-out in that turn. The next one claims the goal, ends the
# turn and posts its handoff and then the goal; the earlier one posts nothing into the turn it ended.
session, head_a = fresh(holds=True)
session.end_turn()
contended = []


def worker(head, goal, handoff):
    held = []
    try:
        remote_control.deliver(session, Log(session, head), handoff, goal, (), 30, 5, lambda line: None,
                               lambda: held.append(remote_control.exclusive(HANDOFFS, session.id, contended.append)),
                               HANDOFFS)
    finally:
        for lock in held:
            lock.close()


first = threading.Thread(target=worker, args=(head_a, "the tests pass", "handoff A"))
first.start()
reached = [wait_for(lambda: "handoff A" in session.prompts())]
second = threading.Thread(target=worker, args=(session.head(), claimed(session), "handoff B"))
second.start()
reached.append(wait_for(lambda: "handoff B" in session.prompts()))
check("the next worker ends the handoff's turn, and posts its own handoff once the earlier one lets go",
      all(reached) and [stopped is None for _, stopped in session.interrupts] == [True, False]
      and session.prompts() == ["/clear", "handoff A", "/clear", "handoff B"], (session.prompts(), session.interrupts))
session.end_turn()
first.join(10)
second.join(10)
check("then the goal the earlier one carried is posted once, after the second /clear, to an idle session",
      session.prompts() == ["/clear", "handoff A", "/clear", "handoff B", "/goal the tests pass"]
      and session.posts[-1][1:] == (True, 2) and all(idle for _, idle, _ in session.posts), session.posts)

# --- the session server ---------------------------------------------------------

BASE = fake_session_server.serve()


def login(*tokens):
    """A login whose stored token reads as each of `tokens` in turn, the last one ever after."""
    reads = iter(tokens)
    last = [tokens[-1]]
    return remote_control.Login(lambda: '{"claudeAiOauth": {"accessToken": "%s"}}' % next(reads, last[0]))


def remote(id):
    return remote_control.Session(f"{BASE}/v1/code/sessions/{id}", login(fake_session_server.TOKEN))


served = remote("cse_served01")
head = served.head()
check("the head is the newest event's sequence, read as one event in descending order",
      head == fake_session_server.LOGS["cse_served01"].head() - 2
      and "sort_order=desc&limit=1" in fake_session_server.REQUESTS["cse_served01"][-1][1])
seq = served.post(remote_control.user_event(served.id, "/clear"))
method, path, auth, platform = fake_session_server.REQUESTS["cse_served01"][-1]
check("a post goes to the session's events with the login as bearer",
      (method, path, auth) == ("POST", "/v1/code/sessions/cse_served01/events", f"Bearer {fake_session_server.TOKEN}"),
      (method, path, auth))
check("and declares Claude Code's CLI platform, without which the session runs no slash command",
      platform == "claude_code_cli", platform)
check("and returns the sequence number the log gave it",
      fake_session_server.LOGS["cse_served01"].prompts() == ["/clear"] and seq > head, seq)
dropping = remote("cse_drops01")
event = remote_control.user_event(dropping.id, "/clear")
error = raised(lambda: dropping.post(event))
check("a post whose answer is lost is a passing failure", isinstance(error, Transient), error)
check("and the same event posted again is answered with the first copy's sequence number, and logged once",
      dropping.post(event) == next(int(taken["sequence_num"]) for taken in fake_session_server.LOGS["cse_drops01"].events
                                   if taken["payload"].get("uuid") == event["payload"]["uuid"])
      and fake_session_server.LOGS["cse_drops01"].prompts() == ["/clear"],
      fake_session_server.LOGS["cse_drops01"].events)
log = Log(served, 0)
log.read()
check("a read over HTTP takes in every page to the last",
      log.cursor == fake_session_server.LOGS["cse_served01"].head()
      and len(fake_session_server.LOGS["cse_served01"].events) > fake_session_server.PAGE and log.idle, vars(log))

expiring = remote_control.Session(f"{BASE}/v1/code/sessions/cse_expires01", login("stale", fake_session_server.TOKEN))
check("a 401 is answered by reading the stored login again, which Claude Code has refreshed",
      raised(expiring.head) is None and expiring.login.token == fake_session_server.TOKEN
      and [r[2] for r in fake_session_server.REQUESTS["cse_expires01"]] == ["Bearer stale", f"Bearer {fake_session_server.TOKEN}"],
      fake_session_server.REQUESTS["cse_expires01"])
error = raised(remote_control.Session(f"{BASE}/v1/code/sessions/cse_expires02", login("stale")).head)
check("and one the re-read login also gets is a passing failure, since Claude Code may not have refreshed it yet",
      isinstance(error, Transient) and "HTTP 401" in str(error), error)
readings = iter(['{"claudeAiOauth": {"accessToken": "stale"}}'])
unreadable = remote_control.Session(f"{BASE}/v1/code/sessions/cse_expires03",
                                    remote_control.Login(lambda: next(readings, "{}")))
error = raised(unreadable.head)
check("as is a 401 whose login cannot be read again, since Claude Code may be writing it",
      isinstance(error, Transient) and "could not be read again" in str(error), error)
error = raised(lambda: remote("cse_refuses01").post(remote_control.user_event("cse_refuses01", "/clear")))
check("a refused post names the status and the server's reason, and is no passing failure",
      type(error) is Refused and "HTTP 403" in str(error) and "fixture refuses" in str(error), error)
flaky = remote("cse_flaky01")
flaky.head()
error = raised(lambda: flaky.page(0))
check("a 5xx is a passing failure", isinstance(error, Transient) and "HTTP 502" in str(error), error)
closed = remote_control.Session("http://127.0.0.1:9/v1/code/sessions/cse_x", login("tok"))
check("so is a server that does not answer", isinstance(raised(closed.head), Transient))
error = raised(remote("cse_garbled01").head)
check("a 2xx that is not JSON is a refusal naming what came", type(error) is Refused and "<html>" in str(error), error)
error = raised(lambda: remote("cse_garbled02").post(remote_control.user_event("cse_garbled02", "/clear")))
check("but for a post it is a passing failure, since the server took the prompt",
      type(error) is Transient and "<html>" in str(error), error)
error = raised(remote("cse_moved01").head)
check("a redirect is refused, so the login is never sent on to where it points",
      type(error) is Refused and "HTTP 302" in str(error) and "cse_elsewhere" not in fake_session_server.REQUESTS,
      error)


class Answering(remote_control.Session):
    """A session whose every request is answered `answer`."""
    def __init__(self, answer):
        super().__init__(f"{BASE}/v1/code/sessions/cse_answering", login("tok"))
        self.answer = answer

    def request(self, method, path, body=None):
        return self.answer


event = remote_control.user_event("cse_answering", "/clear")
check("a post's answer is its sequence number", Answering({"results": [{"sequence_num": "5"}]}).post(event) == 5)
for answer in ({"results": []}, {"results": [{"event_id": "e"}]}):
    error = raised(lambda: Answering(answer).post(event))
    check(f"one with no sequence number is a passing failure, since the server took it: {answer}",
          isinstance(error, Transient) and "returned no sequence number" in str(error), error)

# --- the script, as the launcher runs it ---------------------------------------

script = os.path.join(LIB, "remote_control.py")
# A server's first argument is a script named for the subcommand, run from its directory, so the
# interpreter's argv has the shape a claude binary's does.
verbs = tempfile.mkdtemp(prefix="remote-control-verbs.")
with open(os.path.join(verbs, "remote-control"), "w") as handle:
    handle.write(SLEEPS)
server = started("remote-control", "--spawn", "worktree", cwd=verbs)
spawned = started("-c", SLEEPS, "--print", "--sdk-url", URL, f"--resume={URL}")
daemon = started("-c", SLEEPS, "remote-control")
try:
    done = subprocess.run([sys.executable, "-I", script, "session", str(server.pid), str(spawned.pid)],
                          capture_output=True, text=True)
    check("session prints the URL the live server's live child names",
          done.returncode == 0 and done.stdout.strip() == URL, (done.returncode, done.stdout, done.stderr))
    done = subprocess.run([sys.executable, "-I", script, "session", str(daemon.pid), str(spawned.pid)],
                          capture_output=True, text=True)
    check("session for a claude whose host is no server prints nothing and exits 0",
          done.returncode == 0 and done.stdout.strip() == "", (done.returncode, done.stdout, done.stderr))
    done = subprocess.run([sys.executable, "-I", script, "session", str(server.pid), str(daemon.pid)],
                          capture_output=True, text=True)
    check("session for a server's child that names no session refuses with 2, giving its argv",
          done.returncode == 2 and "gives []" in done.stderr and "'remote-control'" in done.stderr,
          (done.returncode, done.stdout, done.stderr))
finally:
    for process in (server, spawned, daemon):
        process.kill()
missing = os.path.join(tempfile.mkdtemp(prefix="remote-control-handoffs."), "no-such-handoff.md")
done = subprocess.run([sys.executable, "-I", script, "deliver", f"{BASE}/v1/code/sessions/cse_lost01", "10",
                       missing, missing, "1", "1"], capture_output=True, text=True)
check("a handoff file the worker cannot read is logged as nothing posted, naming where it was kept",
      done.returncode == 1 and "the /clear did NOT go through" in done.stdout and missing in done.stdout
      and not fake_session_server.REQUESTS.get("cse_lost01"), (done.returncode, done.stdout, done.stderr))
done = subprocess.run([sys.executable, "-I", script, "deliver", f"{BASE}/v1/code/sessions/cse_lost02", "10",
                       missing, missing, "1d", "1"], capture_output=True, text=True)
check("as is a timeout that is no number",
      done.returncode == 1 and "is no number" in done.stdout and "the /clear did NOT go through" in done.stdout
      and missing in done.stdout and not fake_session_server.REQUESTS.get("cse_lost02"),
      (done.returncode, done.stdout, done.stderr))
goalfile = os.path.join(os.path.dirname(missing), "goal")
with open(goalfile, "w") as handle:
    handle.write("the tests pass")
done = subprocess.run([sys.executable, "-I", script, "deliver", f"{BASE}/v1/code/sessions/cse_lost03", "10",
                       missing, goalfile, "1", "1"], capture_output=True, text=True)
check("and a worker stopped before its first post names the goal it carried, to set by hand",
      done.returncode == 1 and "the /clear did NOT go through" in done.stdout
      and "deliver it by hand once the session is up: /goal the tests pass" in done.stdout, done.stdout)
publication = remote_control.publish(os.path.dirname(missing), "cse_live01", "the tests pass")
done = subprocess.run([sys.executable, "-I", script, "claim", f"{BASE}/v1/code/sessions/cse_live01",
                       os.path.dirname(missing)], capture_output=True, text=True)
check("claim prints the goal a live worker published for the session, and takes it",
      done.returncode == 0 and done.stdout == "the tests pass" and not publication.take_back(lambda line: None),
      (done.returncode, done.stdout, done.stderr))
done = subprocess.run([sys.executable, "-I", script, "claim", f"{BASE}/v1/code/sessions/cse_live01",
                       os.path.dirname(missing)], capture_output=True, text=True)
check("and prints nothing when there is none", done.returncode == 0 and done.stdout == "", done)
# The stored login, as the keychain (a `security` that answers with it) or the config dir's file holds it.
STORED = '{"claudeAiOauth": {"accessToken": "%s"}}' % fake_session_server.TOKEN
login_dir = tempfile.mkdtemp(prefix="remote-control-login.")
with open(os.path.join(login_dir, ".credentials.json"), "w") as handle:
    handle.write(STORED)
with open(os.path.join(login_dir, "security"), "w") as handle:
    handle.write(f"#!/bin/sh\nprintf '%s' '{STORED}'\n")
os.chmod(os.path.join(login_dir, "security"), 0o755)
done = subprocess.run([sys.executable, "-I", script, "head", f"{BASE}/v1/code/sessions/cse_hiccups01"],
                      capture_output=True, text=True,
                      env={**os.environ, "CLAUDE_CONFIG_DIR": login_dir, "PATH": login_dir + ":" + os.environ["PATH"]})
check("head reads through a read that fails in passing, saying so on stderr",
      done.returncode == 0 and done.stdout.strip() == str(fake_session_server.LOGS["cse_hiccups01"].head() - 2)
      and "retrying" in done.stderr and "HTTP 502" in done.stderr, (done.returncode, done.stdout, done.stderr))
done = subprocess.run([sys.executable, "-I", script, "deliver", URL], capture_output=True, text=True)
check("a command missing an argument prints the usage and exits 2",
      done.returncode == 2 and "usage: remote_control.py session <host pid> <claude pid>" in done.stderr
      and "[<setting>...]" in done.stderr, done.stderr)

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
