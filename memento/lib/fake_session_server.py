"""A fake of Claude Code's session server, for the suites that test lib/remote_control.py and the launcher
that runs it. [LAW:one-source-of-truth] It is the one model of a session's event log those suites share.

SessionLog is one session's log as a running Remote Control session writes it, and it answers the calls
remote_control.Session makes, so a test can hand it to the poster in place of a Session. serve() puts the
same logs behind HTTP on localhost. There the session id chooses how a session behaves, so a case differs
only in the id it uses:
  cse_busy...     the asking turn never ends
  cse_stalls...   a /clear brings no reset
  cse_refuses...  every request is answered 403
  cse_flaky...    the first read of the log after the head is answered 502
  anything else   the asking turn ends once the log's head has been read
Every request must carry TOKEN as its bearer, or it is answered 401.
"""

import http.server
import json
import threading

TOKEN = "fixture-login"
# A read returns at most PAGE events and a next_cursor, so following pages is exercised.
PAGE = 3


class SessionLog:
    """One session's log. It starts inside the asking turn: a prompt with no `result` after it.

    A posted slash command runs at once, as a session runs one: it ends in a `result`, and a /clear also in
    a conversation_reset unless `resets` is off. Any other prompt starts a turn, which ends at once unless
    `holds` is on; then end_turn() ends it. Each post records whether the session was idle when it came."""

    def __init__(self, id, resets=True, holds=False):
        self.id, self.resets, self.holds = id, resets, holds
        self.lock = threading.Lock()
        self.events = []
        self.posts = []  # (prompt text, whether the session was idle)
        self.append({"type": "user", "uuid": "asking-turn"}, {"type": "assistant"})

    def append(self, *payloads):
        for payload in payloads:
            self.events.append({"sequence_num": str(10 + len(self.events)), "payload": payload})
        return int(self.events[-1]["sequence_num"])

    def idle(self):
        kinds = [event["payload"]["type"] for event in self.events if event["payload"]["type"] in ("user", "result")]
        return kinds[-1] == "result"

    def end_turn(self):
        with self.lock:
            self.append({"type": "assistant"}, {"type": "result"})

    def post(self, event):
        payload = event["payload"]
        text = payload["message"]["content"]
        with self.lock:
            self.posts.append((text, self.idle()))
            seq = self.append(payload)
            if text == "/clear":
                self.append(*([{"type": "conversation_reset"}] if self.resets else []), {"type": "result"})
            elif text.startswith("/") and not text.startswith("/goal "):
                self.append({"type": "result"})
            elif not self.holds:
                self.append({"type": "assistant"}, {"type": "result"})
        return seq

    def head(self):
        with self.lock:
            return int(self.events[-1]["sequence_num"])

    def page(self, cursor):
        """The events after `cursor`, PAGE at a time, as the server pages them."""
        with self.lock:
            after = [event for event in self.events if int(event["sequence_num"]) > int(cursor)]
        page = {"data": after[:PAGE]}
        if len(after) > PAGE:
            page["next_cursor"] = after[PAGE - 1]["sequence_num"]
        return page

    def events_after(self, cursor):
        events, page = [], self.page(cursor)
        events += page["data"]
        while page.get("next_cursor"):
            page = self.page(page["next_cursor"])
            events += page["data"]
        return events

    def prompts(self):
        return [text for text, _ in self.posts]


# Per session id: its log, and the requests it received as (method, path, Authorization, platform).
LOGS = {}
REQUESTS = {}


def log_of(id):
    if id not in LOGS:
        LOGS[id] = SessionLog(id, resets=not id.startswith("cse_stalls"))
    return LOGS[id]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def refusal(self):
        """The answer a request is refused with, or None when it is served. Records the request."""
        id = self.path.split("/")[4].split("?")[0]
        received = REQUESTS.setdefault(id, [])
        received.append((self.command, self.path, self.headers.get("Authorization"),
                         self.headers.get("anthropic-client-platform")))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"error": {"type": "authentication_error", "message": "token expired"}}
        if id.startswith("cse_refuses"):
            return 403, {"error": {"type": "permission_error", "message": "fixture refuses"}}
        if id.startswith("cse_flaky") and [r[0] for r in received].count("GET") == 2:
            return 502, {"error": {"type": "api_error", "message": "bad gateway"}}
        return None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        refused = self.refusal()
        if refused:
            return self.answer(*refused)
        seq = log_of(self.path.split("/")[4]).post(body["events"][0])
        self.answer(200, {"results": [{"event_id": "e", "sequence_num": str(seq), "duplicate": False}]})

    def do_GET(self):
        refused = self.refusal()
        if refused:
            return self.answer(*refused)
        log = log_of(self.path.split("/")[4].split("?")[0])
        if "sort_order=desc&limit=1" in self.path:
            head = log.head()
            if not log.id.startswith("cse_busy"):
                log.end_turn()
            return self.answer(200, {"data": [{"sequence_num": str(head), "payload": {"type": "assistant"}}]})
        self.answer(200, log.page(self.path.rpartition("cursor=")[2]))


def serve():
    """Starts the fake on localhost and returns its base URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"
