"""A fake of Claude Code's session server, for the suites that test lib/remote_control.py and the launcher
that runs it. [LAW:one-source-of-truth] It is the one model of a session's event log those suites share.

SessionLog is one session's log as a running Remote Control session writes it, and it answers the calls
remote_control.Session makes, so a test can hand it to the poster in place of a Session. serve() puts the
same logs behind HTTP on localhost. There the session id chooses how a session behaves, so a case differs
only in the id it uses:
  cse_busy...     the asking turn never ends, and an interrupt stops nothing
  cse_held...     a goal holds the asking turn working, so only an interrupt aimed at it ends it
  cse_stalls...   a /clear brings no reset
  cse_refuses...  every request is answered 403
  cse_flaky...    the first read of the log after the head is answered 502
  cse_hiccups...  the first request is answered 502
  cse_drops...    the first post is taken into the log and its connection closed with no answer
  cse_garbled...  every request is answered 200 with a page of HTML
  cse_moved...    every read is redirected to cse_elsewhere
  anything else   the asking turn ends once the log's head has been read
A read back through the log, a desc read with a cursor, ends nothing.
Every request must carry TOKEN as its bearer, or it is answered 401.
"""

import http.server
import json
import threading
import urllib.parse

TOKEN = "fixture-login"
# A read returns at most PAGE events and a next_cursor, so following pages is exercised.
PAGE = 3
# The longest condition /goal takes (2.1.299).
GOAL_LIMIT = 4000


class SessionLog:
    """One session's log. It starts inside the asking turn, whose prompt is "asking-turn": a prompt with no
    `result` after it.

    It models what lib/remote_control.py reads of a 2.1.299 session:
    - A posted /clear runs at once: it ends in a `result`, and also in a conversation_reset unless `resets`
      is off, and it ends the goal.
    - `/goal <c>` sets the goal and starts a turn of its own, as the command does. One over GOAL_LIMIT
      characters is answered as text, with a `result` and no turn.
    - Any other slash command ends at once. Any other prompt starts a turn.
    - A turn ends at once, unless `holds` is on or a goal is in force, which is the goal's Stop hook; then
      end_turn() ends it.
    - A turn's first top-level assistant event is stamped with its prompt's uuid. An asking turn with
      `stamped` off is a task notification's: unstamped, and keeping the turn before it as its identity.
      With `late` on, its stamp is uploaded only when upload_stamp() is called.
    - An interrupt whose for_user_message_uuids name the running turn ends it in a `result`, with the
      goal kept; one naming no running turn stops nothing, as does every one when `deaf` is on.

    A post whose uuid the log already holds is dropped, answered with the first one's sequence number and
    as a duplicate, as the server does: post() answers what remote_control.Session.post does, a sequence
    number and whether the post was a duplicate.

    It records each post with whether the session was idle and how many resets came before it, each
    prompt's turn with the goal in force when it started, and each interrupt with the turn it stopped."""

    def __init__(self, id, resets=True, holds=False, goal=None, stamped=True, late=False, deaf=False):
        self.id, self.resets, self.holds, self.goal, self.deaf = id, resets, holds, goal, deaf
        self.lock = threading.Lock()
        self.events = []
        self.posts = []  # (prompt text, whether the session was idle, resets before it)
        self.turns = []  # (prompt text, the goal in force when its turn started)
        self.interrupts = []  # (the prompts an interrupt named, the running turn it stopped or None)
        self.reset_count = 0
        self.running = None  # the prompts of the running turn, or None when idle
        if stamped:
            self.running, self.pending = ("asking-turn",), None
            self.append({"type": "user", "uuid": "asking-turn"})
            if late:
                self.pending = self.stamp(self.running)
            else:
                self.append(self.stamp(self.running))
        else:
            self.append({"type": "user", "uuid": "previous-turn"}, self.stamp(("previous-turn",)),
                        {"type": "result"}, {"type": "system", "subtype": "task_notification"},
                        {"type": "assistant", "parent_tool_use_id": None})
            self.running, self.pending = ("previous-turn",), None

    @staticmethod
    def stamp(prompts):
        return {"type": "assistant", "parent_tool_use_id": None, "user_message_uuid": prompts[0],
                "user_message_uuids": list(prompts)}

    def append(self, *payloads):
        for payload in payloads:
            self.events.append({"sequence_num": str(10 + len(self.events)), "payload": payload})
        return int(self.events[-1]["sequence_num"])

    def idle(self):
        return self.running is None

    def upload_stamp(self):
        with self.lock:
            self.append(self.pending)

    def end_turn(self):
        with self.lock:
            self.running = None
            self.append({"type": "assistant", "parent_tool_use_id": None}, {"type": "result"})

    def begin(self, prompt, text):
        self.turns.append((text, self.goal))
        self.running = (prompt,)
        self.append(self.stamp(self.running))

    def post(self, event):
        payload = event["payload"]
        with self.lock:
            for earlier in self.events:
                if earlier["payload"].get("uuid") == payload["uuid"]:
                    return int(earlier["sequence_num"]), True
            if payload["type"] == "control_request":
                seq = self.append(payload)
                named = payload["request"]["for_user_message_uuids"]
                stopped = self.running if self.running and set(named) & set(self.running) and not self.deaf else None
                self.interrupts.append((tuple(named), stopped))
                if stopped:
                    self.running = None
                    self.append({"type": "result", "terminal_reason": "aborted_streaming"})
                return seq, False
            text = payload["message"]["content"]
            self.posts.append((text, self.idle(), self.reset_count))
            seq = self.append(payload)
            if text == "/clear":
                self.goal, self.running = None, None
                self.reset_count += 1
                self.append(*([{"type": "conversation_reset"}] if self.resets else []), {"type": "result"})
            elif text.startswith("/goal "):
                if len(text) - len("/goal ") > GOAL_LIMIT:
                    self.append({"type": "assistant", "parent_tool_use_id": None, "local_command_source": "goal"},
                                {"type": "result"})
                else:
                    self.goal = text[len("/goal "):]
                    self.begin(payload["uuid"], text)
            elif text.startswith("/"):
                self.append({"type": "result"})
            else:
                self.begin(payload["uuid"], text)
                if not self.holds and self.goal is None:
                    self.running = None
                    self.append({"type": "result"})
        return seq, False

    def head(self):
        with self.lock:
            return int(self.events[-1]["sequence_num"])

    def page(self, after):
        """The first PAGE events after sequence number `after`, and whether more follow them."""
        with self.lock:
            later = [event for event in self.events if int(event["sequence_num"]) > int(after)]
        return later[:PAGE], len(later) > PAGE

    def history(self, before):
        """The first PAGE events before sequence number `before`, newest first, and whether more precede
        them."""
        with self.lock:
            earlier = [event for event in reversed(self.events) if int(event["sequence_num"]) < int(before)]
        return earlier[:PAGE], len(earlier) > PAGE

    def prompts(self):
        return [text for text, _, _ in self.posts]


# Per session id: its log, and the requests it received as (method, path, Authorization, platform).
LOGS = {}
REQUESTS = {}


def log_of(id):
    if id not in LOGS:
        LOGS[id] = SessionLog(id, resets=not id.startswith("cse_stalls"), deaf=id.startswith("cse_busy"),
                              goal="the old goal" if id.startswith("cse_held") else None)
    return LOGS[id]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def sign_in(self):
        """A 200 that is not JSON, as a proxy's sign-in page is."""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>sign in</html>")

    def session(self):
        """The session id the request's path names: /v1/code/sessions/<id>/events."""
        return urllib.parse.urlparse(self.path).path.split("/")[4]

    def refusal(self, id):
        """The answer a request is refused with, or None when it is served. Records the request."""
        received = REQUESTS.setdefault(id, [])
        received.append((self.command, self.path, self.headers.get("Authorization"),
                         self.headers.get("anthropic-client-platform")))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"error": {"type": "authentication_error", "message": "token expired"}}
        if id.startswith("cse_refuses"):
            return 403, {"error": {"type": "permission_error", "message": "fixture refuses"}}
        if id.startswith("cse_flaky") and [r[0] for r in received].count("GET") == 2:
            return 502, {"error": {"type": "api_error", "message": "bad gateway"}}
        if id.startswith("cse_hiccups") and len(received) == 1:
            return 502, {"error": {"type": "api_error", "message": "bad gateway"}}
        return None

    def do_POST(self):
        body, id = json.loads(self.rfile.read(int(self.headers["Content-Length"]))), self.session()
        refused = self.refusal(id)
        if refused:
            return self.answer(*refused)
        if id.startswith("cse_garbled"):
            return self.sign_in()
        seq, duplicate = log_of(id).post(body["events"][0])
        if id.startswith("cse_drops") and [r[0] for r in REQUESTS[id]].count("POST") == 1:
            # A lost answer: the connection closes with the event taken and nothing said.
            self.close_connection = True
            return
        self.answer(200, {"results": [{"event_id": "e", "sequence_num": str(seq), "duplicate": duplicate}]})

    def do_GET(self):
        id = self.session()
        refused = self.refusal(id)
        if refused:
            return self.answer(*refused)
        if id.startswith("cse_moved"):
            self.send_response(302)
            self.send_header("Location", "/v1/code/sessions/cse_elsewhere/events")
            return self.end_headers()
        if id.startswith("cse_garbled"):
            return self.sign_in()
        log, query = log_of(id), urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if query.get("sort_order") == ["desc"] and "cursor" not in query:
            newest = log.history(log.head() + 1)[0][:1]
            if not log.id.startswith(("cse_busy", "cse_held")):
                log.end_turn()
            return self.answer(200, {"data": newest})
        events, more = (log.history if query.get("sort_order") == ["desc"] else log.page)(query["cursor"][0])
        # The server's next_cursor is its own token; a client reads it only for whether more follow.
        self.answer(200, {"data": events, **({"next_cursor": "opaque+token"} if more else {})})


def serve():
    """Starts the fake on localhost and returns its base URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"
