#!/usr/bin/env python3
"""Tests for github_threads' pagination and response-shape handling.

Each test builds a `GitHub` client around a fake Runner that pages, and asserts that the whole set
is returned. Argv construction, cursor threading, and JSON and JSONL parsing all run for real. No test
uses the network, and the fake refuses any argv it was not taught. Run with: python3
github_threads.test.py
"""

import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "github_threads", os.path.join(HERE, "github_threads.py")
)
gt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gt)
sys.path.insert(0, HERE)
from fakes import FakeRunner, failed  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def check_raises(name, exc_type, fn, must_mention=()):
    try:
        fn()
    except exc_type as e:
        missing = [m for m in must_mention if m not in str(e)]
        check(name, not missing, f"message {str(e)!r} omits {missing}")
    except BaseException as e:  # noqa: BLE001 - the wrong error type IS the failure
        check(name, False, f"raised {type(e).__name__}: {e}")
    else:
        check(name, False, "did not raise")


def check_returns(name, fn, expected):
    """Runs `fn` and reports an exception as a failed check instead of aborting the run."""
    try:
        got = fn()
    except BaseException as e:  # noqa: BLE001 - raising IS the failure here
        check(name, False, f"raised {type(e).__name__}: {e}")
    else:
        check(name, got == expected, f"got {got!r}, want {expected!r}")


# --- the fake gh ----------------------------------------------------------


def install(handler):
    """Returns a client over a fresh fake, and the fake, so a check can read its calls."""
    fake = FakeRunner(handler)
    return gt.GitHub(fake), fake


def gh_vars(args):
    """Returns the `-f`/`-F key=value` pairs from a gh argv."""
    out = {}
    i = 0
    while i < len(args):
        if args[i] in ("-f", "-F"):
            key, _, value = args[i + 1].partition("=")
            out[key] = value
            i += 2
        else:
            i += 1
    return out


# --- fixtures -------------------------------------------------------------


def comment(login, body):
    return {"author": {"login": login}, "body": body}


def thread_node(tid, comments, has_next=False, end=None):
    return {
        "id": tid, "isResolved": False, "path": "a.py", "line": 7,
        "comments": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": end},
            "nodes": comments,
        },
    }


def threads_page(nodes, has_next=False, end=None):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": end},
        "nodes": nodes,
    }}}}}


def comments_page(nodes, has_next=False, end=None):
    return {"data": {"node": {"comments": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": end},
        "nodes": nodes,
    }}}}


def paged_graphql(thread_pages, comment_pages=None):
    """Returns a fake gh whose GraphQL answers are keyed by cursor.

    An absent cursor variable selects the first page. A cursor sent as an empty string is refused.
    """
    comment_pages = comment_pages or {}

    def handler(args):
        if args[:2] != ["api", "graphql"]:
            raise AssertionError(f"unexpected gh call: {args}")
        variables = gh_vars(args)
        if "cursor" in variables and variables["cursor"] == "":
            raise AssertionError("cursor sent as empty string, not omitted")
        cursor = variables.get("cursor")
        pages = thread_pages if "reviewThreads(" in variables["query"] else comment_pages
        if cursor not in pages:
            raise AssertionError(f"no fixture page for cursor {cursor!r}")
        return json.dumps(pages[cursor])

    return handler


PR = "https://github.com/o/r/pull/7"


# --- multi-page thread fetch ---------------------------------------------

github, fake = install(paged_graphql({
    None: threads_page([thread_node("t1", [comment("bot", "one")]),
                        thread_node("t2", [comment("bot", "two")])],
                       has_next=True, end="cursor-A"),
    "cursor-A": threads_page([thread_node("t3", [comment("bot", "three")])],
                             has_next=True, end="cursor-B"),
    "cursor-B": threads_page([thread_node("t4", [comment("bot", "four")])]),
}))
findings = github.fetch(PR)["findings"]
check("threads: every page accumulates",
      [f["thread_id"] for f in findings] == ["t1", "t2", "t3", "t4"],
      f'got {[f["thread_id"] for f in findings]}')
check("threads: last page's content survives",
      findings[-1]["body"] == "four", f"got {findings[-1]['body']!r}")
check("threads: first request omits the cursor variable",
      "cursor" not in gh_vars(fake.calls[0]), f"got {fake.calls[0]}")
check("threads: later requests thread the endCursor through",
      [gh_vars(c).get("cursor") for c in fake.calls[:3]] == [None, "cursor-A", "cursor-B"],
      f"got {[gh_vars(c).get('cursor') for c in fake.calls[:3]]}")


# --- multi-page comment fetch inside _complete_comments -------------------

github, fake = install(paged_graphql(
    thread_pages={
        None: threads_page([thread_node(
            "t1", [comment("bot", "c1")], has_next=True, end="cc-A")]),
    },
    comment_pages={
        "cc-A": comments_page([comment("human", "c2")], has_next=True, end="cc-B"),
        "cc-B": comments_page([comment("bot", "c3")]),
    },
))
bodies = [c["body"] for c in github.fetch(PR)["findings"][0]["thread_comments"]]
check("comments: every page accumulates onto the thread",
      bodies == ["c1", "c2", "c3"], f"got {bodies}")


# --- cursor omission is end-of-pages, not a page to fetch -----------------
# A final page with hasNextPage false and no endCursor ends the walk without an error.

github, fake = install(paged_graphql({
    None: threads_page([thread_node("t1", [comment("bot", "one")])], has_next=True,
                       end="cursor-A"),
    "cursor-A": {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False},
        "nodes": [thread_node("t2", [comment("bot", "two")])],
    }}}}},
}))
findings = github.fetch(PR)["findings"]
check("cursor omission: loop terminates on the endCursor-less last page",
      [f["thread_id"] for f in findings] == ["t1", "t2"],
      f'got {[f["thread_id"] for f in findings]}')
check("cursor omission: no extra page was requested",
      len(fake.calls) == 2, f"made {len(fake.calls)} gh calls: {fake.calls}")


# --- hasNextPage false is the whole answer, cursor present or not ----------
# GitHub's final pages also carry a valid endCursor, so this case must not be the only one: the
# walk ends on hasNextPage, not on the cursor's presence. Both loops are checked with a final page
# that has hasNextPage false and a live cursor.

github, fake = install(paged_graphql({
    None: threads_page([thread_node("t1", [comment("bot", "one")],
                                    has_next=False, end="comment-cursor-Z")],
                       has_next=False, end="thread-cursor-Z"),
}))
findings = github.fetch(PR)["findings"]
check("terminal page: hasNextPage false ends the walk even with a live cursor",
      [f["thread_id"] for f in findings] == ["t1"],
      f'got {[f["thread_id"] for f in findings]}')
check("terminal page: the live cursor was not followed",
      len(fake.calls) == 1, f"made {len(fake.calls)} gh calls: {fake.calls}")


# --- a promised page nobody named is a loud error, never a quiet stop ------
# hasNextPage true with no endCursor must raise and name the PR. Each case below also asserts that
# no further page was requested.

for label, bad_page_info in [
    ("endCursor null", {"hasNextPage": True, "endCursor": None}),
    ("endCursor absent", {"hasNextPage": True}),
    ("endCursor empty string", {"hasNextPage": True, "endCursor": ""}),
]:
    github, fake = install(lambda args, pi=bad_page_info: json.dumps(
        {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": pi,
            "nodes": [thread_node("t1", [comment("bot", "one")])],
        }}}}}
    ))
    check_raises(f"threads: {label} with hasNextPage true raises",
                 RuntimeError, lambda: github.fetch(PR),
                 must_mention=("hasNextPage", "endCursor", "o/r#7"))
    check(f"threads: {label} stops before re-requesting page one",
          len(fake.calls) == 1, f"made {len(fake.calls)} gh calls: {fake.calls}")


def unnamed_comment_page(args):
    """Returns a well-formed first thread page whose comments promise a next page that is never named."""
    variables = gh_vars(args)
    if "reviewThreads(" in variables["query"]:
        return json.dumps(threads_page([thread_node(
            "t1", [comment("bot", "c1")], has_next=True, end=None)]))
    raise AssertionError(f"comment page requested with cursor {variables.get('cursor')!r}")


github, fake = install(unnamed_comment_page)
check_raises("comments: a promised page with no endCursor raises",
             RuntimeError, lambda: github.fetch(PR),
             must_mention=("hasNextPage", "endCursor", "t1"))
check("comments: no comment page was requested with a null cursor",
      len(fake.calls) == 1, f"made {len(fake.calls)} gh calls: {fake.calls}")


# --- --paginate + --jq JSONL concatenation in change_requests -------------
# gh applies --jq per page and concatenates the output as JSONL, with a newline per page, so blank
# lines must be skipped while parsing.

def reviews_handler(args):
    if args[:2] != ["api", "--paginate"]:
        raise AssertionError(f"unexpected gh call: {args}")
    if args[2] != "repos/o/r/pulls/7/reviews":
        raise AssertionError(f"unexpected endpoint: {args[2]}")
    page_one = ('{"review_id": 11, "author": "bot", "commit_id": "aaa", '
                '"state": "CHANGES_REQUESTED", "body": "a\\nb"}\n'
                '{"review_id": 12, "author": "bot", "commit_id": "aaa", '
                '"state": "COMMENTED", "body": "<!-- marker -->"}\n')
    page_two = ('{"review_id": 13, "author": "bot", "commit_id": "bbb", '
                '"state": "CHANGES_REQUESTED", "body": "c"}\n')
    return (page_one + "\n" + page_two).strip()


github, fake = install(reviews_handler)
reviews = github.bot_reviews(PR)
check("bot_reviews: JSONL from every page is parsed, every state kept",
      [(r["review_id"], r["state"]) for r in reviews]
      == [(11, "CHANGES_REQUESTED"), (12, "COMMENTED"), (13, "CHANGES_REQUESTED")],
      f"got {reviews}")
check("bot_reviews: asks gh to walk every page",
      "--paginate" in fake.calls[0], f"got {fake.calls[0]}")
reviews = github.change_requests(PR)["reviews"]
check("change_requests: only the blocking reviews, in the contract's projection",
      reviews == [{"review_id": 11, "author": "bot", "commit_id": "aaa"},
                  {"review_id": 13, "author": "bot", "commit_id": "bbb"}],
      f"got {reviews}")
check("change_requests: its own gh call walks every page",
      len(fake.calls) == 2 and "--paginate" in fake.calls[1], f"got {fake.calls}")

# change_requests and action_provider.body_findings share one blocking-review rule.
check("is_blocking_review: CHANGES_REQUESTED blocks, no other state does",
      gt.is_blocking_review({"state": "CHANGES_REQUESTED"})
      and not gt.is_blocking_review({"state": "COMMENTED"})
      and not gt.is_blocking_review({"state": "DISMISSED"})
      and not gt.is_blocking_review({"state": "APPROVED"}),
      "predicate")


# --- the jq filter itself, through a real jq -------------------------------
# The fake gh returns what the test wrote, so this check runs the real jq filter over a raw reviews
# page to catch a regression in the filter. jq is required: a skipped check would pass silently.

import shutil  # noqa: E402
import subprocess  # noqa: E402

check("jq filter: jq is installed (brew install jq)", shutil.which("jq") is not None)
raw_page = [
    {"id": 21, "user": {"login": "github-actions[bot]", "type": "Bot"},
     "state": "COMMENTED", "commit_id": "aaa", "body": "reviewed\n<!-- m -->"},
    {"id": 22, "user": {"login": "alice", "type": "User"},
     "state": "CHANGES_REQUESTED", "commit_id": "aaa", "body": "human"},
    {"id": 23, "user": {"login": "github-actions[bot]", "type": "Bot"},
     "state": "CHANGES_REQUESTED", "commit_id": "bbb", "body": "blocking"},
]


def through_jq():
    out = subprocess.run(
        ["jq", "-c", gt._BOT_REVIEWS_JQ], input=json.dumps(raw_page), text=True,
        capture_output=True, check=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# Behind check_returns so a missing jq fails THIS check and the rest of the
# suite still runs and reports.
check_returns("jq filter: keeps every Bot review whatever its state, drops the human, "
              "emits the bot_reviews projection one object per line",
              through_jq,
              [{"review_id": 21, "author": "github-actions[bot]", "commit_id": "aaa",
                "state": "COMMENTED", "body": "reviewed\n<!-- m -->"},
               {"review_id": 23, "author": "github-actions[bot]", "commit_id": "bbb",
                "state": "CHANGES_REQUESTED", "body": "blocking"}])


# --- response-shape helper: a null at each level names that field ----------

def null_at(*, repository=..., pull_request=..., review_threads=...):
    inner = {}
    if review_threads is not ...:
        inner["reviewThreads"] = review_threads
    pr = inner if pull_request is ... else pull_request
    repo = {"pullRequest": pr} if repository is ... else repository
    return lambda args: json.dumps({"data": {"repository": repo}})


for field, handler in [
    ("data.repository", null_at(repository=None)),
    ("data.repository.pullRequest", null_at(pull_request=None)),
    ("data.repository.pullRequest.reviewThreads", null_at(review_threads=None)),
]:
    github, fake = install(handler)
    check_raises(f"shape: null {field} raises a diagnosable error",
                 RuntimeError, lambda: github.fetch(PR), must_mention=(field, "o/r#7"))

github, fake = install(lambda args: json.dumps({"notdata": {}}))
check_raises("shape: null data raises a diagnosable error",
             RuntimeError, lambda: github.fetch(PR), must_mention=("data",))


def null_node_handler(args):
    variables = gh_vars(args)
    if "reviewThreads(" in variables["query"]:
        return json.dumps(threads_page([thread_node(
            "t1", [comment("bot", "c1")], has_next=True, end="cc-A")]))
    return json.dumps({"data": {"node": None}})


github, fake = install(null_node_handler)
check_raises("shape: null data.node in the comment page names that field",
             RuntimeError, lambda: github.fetch(PR),
             must_mention=("data.node", "t1"))


# --- the verified mutations: a no-op must not pass as done ----------------
# resolve() and dismiss_review() are checked for both outcomes: a confirmed response returns, and an
# unconfirmed one raises, naming the thread or review it acted on.


def resolve_response(payload):
    return json.dumps({"data": {"resolveReviewThread": payload}})


def resolve_handler(response, thread_id="T_abc"):
    def handler(args):
        if args[:2] != ["api", "graphql"]:
            raise AssertionError(f"unexpected gh call: {args}")
        variables = gh_vars(args)
        if "resolveReviewThread" not in variables["query"]:
            raise AssertionError(f"not the resolve mutation: {variables['query']!r}")
        if variables["id"] != thread_id:
            raise AssertionError(f"resolving the wrong thread: {variables['id']!r}")
        # `-F` would type-infer an all-digit id into a number, so an id is always sent raw.
        if args[args.index(f"id={thread_id}") - 1] != "-f":
            raise AssertionError(f"thread id not sent with -f: {args}")
        return response

    return handler


CONFIRMED = resolve_response({"thread": {"isResolved": True}})

github, fake = install(resolve_handler(CONFIRMED))
check_returns("resolve: a confirmed resolution returns the resolved thread",
              lambda: github.resolve("T_abc"),
              {"thread_id": "T_abc", "is_resolved": True})

github, fake = install(resolve_handler(CONFIRMED, thread_id="12345"))
check_returns("resolve: an all-digit thread id is sent as a string",
              lambda: github.resolve("12345"),
              {"thread_id": "12345", "is_resolved": True})

for label, response in [
    ("false", resolve_response({"thread": {"isResolved": False}})),
    ("null", resolve_response({"thread": {"isResolved": None}})),
    ("absent thread", resolve_response({"thread": None})),
    ("absent payload", resolve_response(None)),
]:
    github, fake = install(resolve_handler(response))
    check_raises(f"resolve: confirmation {label} raises and names the thread",
                 RuntimeError, lambda: github.resolve("T_abc"),
                 must_mention=("T_abc", "NOT resolved"))


for label, out in [("empty", ""), ("not JSON", "<html>"), ("null", "null"), ("a list", "[]")]:
    github, fake = install(resolve_handler(out))
    check_raises(f"graphql: a response that is {label} raises RuntimeError quoting it",
                 RuntimeError, lambda: github.resolve("T_abc"), must_mention=(repr(out),))


def reply_response(payload):
    return json.dumps({"data": {"addPullRequestReviewThreadReply": payload}})


def reply_handler(response, thread_id="T_abc", body="Plan: fix"):
    def handler(args):
        if args[:2] != ["api", "graphql"]:
            raise AssertionError(f"unexpected gh call: {args}")
        variables = gh_vars(args)
        if "addPullRequestReviewThreadReply" not in variables["query"]:
            raise AssertionError(f"not the reply mutation: {variables['query']!r}")
        if (variables["id"], variables["body"]) != (thread_id, body):
            raise AssertionError(f"wrong thread or body: {variables!r}")
        # `-F` would read `@path` as a file and type-infer digits, so both are always sent raw.
        for field in (f"id={thread_id}", f"body={body}"):
            if args[args.index(field) - 1] != "-f":
                raise AssertionError(f"{field!r} not sent with -f: {args}")
        return response

    return handler


POSTED = reply_response({"comment": {"id": "PRRC_1"}})

github, fake = install(reply_handler(POSTED))
check_returns("reply: a created comment returns its id",
              lambda: github.reply("T_abc", "Plan: fix"),
              {"thread_id": "T_abc", "comment_id": "PRRC_1"})

github, fake = install(reply_handler(POSTED, thread_id="12345", body="@notes.txt"))
check_returns("reply: an all-digit id and an @-prefixed body are sent as strings",
              lambda: github.reply("12345", "@notes.txt"),
              {"thread_id": "12345", "comment_id": "PRRC_1"})

for label, response in [
    ("null id", reply_response({"comment": {"id": None}})),
    ("absent comment", reply_response({"comment": None})),
    ("absent payload", reply_response(None)),
]:
    github, fake = install(reply_handler(response))
    check_raises(f"reply: {label} raises and names the thread",
                 RuntimeError, lambda: github.reply("T_abc", "Plan: fix"),
                 must_mention=("T_abc", "NOT posted"))


# --- a failed gh call: the error says why, and does not echo the reply ---

GH_STDERR = "GraphQL: Could not resolve to a node with the global id of 'T_gone'. (node)"


def failing_gh(args):
    return failed(GH_STDERR + "\n")


github, fake = install(failing_gh)
check_raises("gh failure: resolve raises GhError carrying gh's stderr",
             gt.GhError, lambda: github.resolve("T_gone"),
             must_mention=("gh api graphql exited 1", GH_STDERR))

github, fake = install(failing_gh)
try:
    github.reply("T_gone", "a long reply body that must not be echoed")
except gt.GhError as e:
    check("gh failure: reply's GhError carries gh's stderr and not the body",
          GH_STDERR in str(e) and "must not be echoed" not in str(e), f"message {str(e)!r}")
else:
    check("gh failure: reply's GhError carries gh's stderr and not the body", False, "did not raise")

github, fake = install(failing_gh)
check_raises("gh failure: a flag before the endpoint does not hide the endpoint",
             gt.GhError, lambda: github.paginated("repos/o/r/pulls/5/reviews", ".[]"),
             must_mention=("gh api --paginate repos/o/r/pulls/5/reviews exited 1",))


def failing_second_page(args):
    page_one = '{"review_id": 1, "body": "a long review body read before the failure"}'
    refusal = '{"message":"Validation Failed","errors":[{"code":"invalid"}],"documentation_url":"https://x"}'
    return failed("gh: Validation Failed (HTTP 422)\n", stdout=f"{page_one}\n{refusal}")


github, fake = install(failing_second_page)
try:
    github.paginated("repos/o/r/pulls/5/reviews", ".[]")
except gt.GhError as e:
    check("gh failure: carries the refusal's message and errors, not the pages read before it",
          "'message': 'Validation Failed'" in str(e) and "'code': 'invalid'" in str(e)
          and "read before the failure" not in str(e) and "documentation_url" not in str(e),
          f"message {str(e)!r}")
else:
    check("gh failure: carries the refusal's message and errors, not the pages read before it",
          False, "did not raise")


def silent_gh(args):
    return failed("")


github, fake = install(silent_gh)
check_raises("gh failure: an empty stderr is said, not left blank",
             gt.GhError, lambda: github.head_sha("o", "r", 5),
             must_mention=("gh api repos/o/r/pulls/5 exited 1: (no stderr)",))


def missing_gh(args):
    return FileNotFoundError(2, "No such file or directory", "gh")


github, fake = install(missing_gh)
check_raises("gh missing: raises GhError, not FileNotFoundError",
             gt.GhError, lambda: github.head_sha("o", "r", 5),
             must_mention=("gh api repos/o/r/pulls/5 could not be run", "No such file or directory"))
try:
    github.head_sha("o", "r", 5)
except gt.GhError as e:
    check("gh missing: GhError chains the OSError",
          isinstance(e.__cause__, FileNotFoundError), repr(e.__cause__))

# The real Runner, with no gh on PATH: it returns the OSError rather than raising it.
_saved_path = os.environ["PATH"]
with tempfile.TemporaryDirectory() as empty_dir:
    os.environ["PATH"] = empty_dir  # an empty PATH would search the cwd
    try:
        outcome = gt.run_gh(["--version"])
    finally:
        os.environ["PATH"] = _saved_path
check("run_gh: a gh that cannot start comes back as the OSError",
      isinstance(outcome, FileNotFoundError), repr(outcome))


def dismiss_handler(state):
    def handler(args):
        if args[:4] != ["api", "--method", "PUT",
                        "repos/o/r/pulls/7/reviews/11/dismissals"]:
            raise AssertionError(f"unexpected gh call: {args}")
        if gh_vars(args)["message"] != "addressed":
            raise AssertionError(f"message not forwarded: {args}")
        return state

    return handler


github, fake = install(dismiss_handler("DISMISSED"))
check_returns("dismiss_review: a confirmed dismissal returns the review",
              lambda: github.dismiss_review(PR, 11, "addressed"),
              {"review_id": 11, "is_dismissed": True})

for label, state in [("still pending", "CHANGES_REQUESTED"), ("empty", "")]:
    github, fake = install(dismiss_handler(state))
    check_raises(f"dismiss_review: state {label} raises and names the review",
                 RuntimeError, lambda: github.dismiss_review(PR, 11, "addressed"),
                 must_mention=("11", "still blocks"))


print(f"\n{len(failures)} failing" if failures else "\nall passing")
sys.exit(1 if failures else 0)
