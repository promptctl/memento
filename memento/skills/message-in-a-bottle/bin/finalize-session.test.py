#!/usr/bin/env python3
"""Tests for finalize-session's tmux pane discovery.

Each case runs the launcher as an agent does (argv in, `--dry-run` report out)
and asserts which transport and pane it selected. The process tree is real:
`nest` builds a chain of genuine processes above the launcher, so the walk runs
against real ps output, pids, and elapsed times. tmux is the only fixture, and the
ps wrapper in the pid-reuse case forges one ancestor's age while delegating
everything else to the real ps. Every chain plants a process whose executable is
named `claude` and exports its pid as $CLAUDE_PID, as Claude Code does, so the walk
finds a process this suite owns rather than whatever claude runs on the host. The
relaunching transports read the permission mode and effort from a transcript each
case writes under its own CLAUDE_CONFIG_DIR.

Run: python3 finalize-session.test.py
"""

import concurrent.futures
import json
import os
import shlex
import ast
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(HERE, "finalize-session")
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(HERE))))
# The journal's command, as the launcher names it in a refusal, the hook that respells a
# launcher command, and the hook that journals the wake of a session a handoff reaches.
JOURNAL_BIN = os.path.join(PLUGIN_ROOT, "skills", "journal", "bin")
JOURNAL_CLI = os.path.join(JOURNAL_BIN, "journal")
HOOK = os.path.join(PLUGIN_ROOT, "hooks", "scripts", "close-out-call.py")
WAKE_HOOK = os.path.join(PLUGIN_ROOT, "hooks", "scripts", "journal-wake.py")
# A plugin is a directory rather than an installed package, so the path comes before the import.
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "lib"))
import hermetic  # noqa: E402

HOME = hermetic.home()
from journal import Close, Summary, chain, entries  # noqa: E402

REAL_DIRS = "/usr/bin:/bin:/usr/sbin:/sbin"
failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


# --- fixtures -------------------------------------------------------------
# A pane record is `id pid dead`. ROOTPID in the pid column stands for the
# top-of-chain pid that `nest` publishes, a real ancestor of the launcher.

TMUX = r"""#!/bin/bash
# Fixture tmux. $FIXTURE_PANES holds one `id pid dead` record per line - panes as
# data - and list-panes EXPANDS whatever -F format it is handed, the way real
# tmux does, so these tests do not care which fields the launcher asks for. An
# unexpanded #{...} left over means the launcher asked for something this fixture
# cannot answer; that exits loudly rather than handing back a line the launcher
# would parse into a plausible wrong pane.
# $FIXTURE_PANES unset stands for no server, which real tmux reports by exiting 1.
set -uo pipefail
case "${1:-}" in
  list-panes)
    [ -n "${FIXTURE_PANES+set}" ] || { echo "no server running" >&2; exit 1; }
    [ -n "$FIXTURE_PANES" ] || exit 0
    fmt=""
    while [ $# -gt 0 ]; do
      case "$1" in -F) fmt="${2:-}"; shift 2 ;; *) shift ;; esac
    done
    [ -n "$fmt" ] || { echo "fixture tmux: list-panes without -F" >&2; exit 2; }
    root=$(cat "$FIXTURE_ROOT_PID")
    while read -r id pid dead; do
      [ -n "$id" ] || continue
      line="${fmt//\#\{pane_id\}/$id}"
      line="${line//\#\{pane_pid\}/${pid//ROOTPID/$root}}"
      line="${line//\#\{pane_dead\}/$dead}"
      case "$line" in
        *'#{'*) echo "fixture tmux: cannot expand '$fmt'" >&2; exit 2 ;;
      esac
      printf '%s\n' "$line"
    done <<< "$FIXTURE_PANES"
    ;;
  new-session|display-message|capture-pane|set-option|load-buffer|paste-buffer|send-keys|kill-session)
    # The detached WORKER's half of the fixture: one session, its state in
    # $FIXTURE_TMUX_STATE. new-session really runs the command line it is given,
    # through sh as tmux's default-shell does, so the relaunch line the worker built
    # (its env prefix, its quoting, its `$(cat msg)`) is what runs. The pane is that
    # command's output; the pane is dead once the command has exited, and its exit
    # status is kept, as remain-on-exit keeps it.
    # $FIXTURE_TMUX_SERVER_CONFIG_DIR stands for a tmux server's own environment: the
    # command starts with CLAUDE_CONFIG_DIR set to it, or unset when it is empty, and
    # only the relaunch line's own prefix can undo that.
    # $FIXTURE_TMUX_FAIL names one subcommand to fail instead, which is how a goal
    # delivery that dies mid-way, or a session that cannot be created at all, is
    # staged. Every subcommand appends its whole argv to $FIXTURE_TMUX_LOG, which is
    # how a case can see that a teardown the worker owes actually happened: a log line
    # from the worker only says it meant to. A buffer loaded from stdin is logged as
    # `buffer: <its text>` after its argv, so a case sees what a paste typed.
    # The tmux WORKER resets a pane that already exists and runs nothing in it, so its
    # cases state what that pane shows: $FIXTURE_TMUX_PANE_TEXT, when set, is what
    # capture-pane prints.
    sub="$1"
    if [ "${FIXTURE_TMUX_FAIL:-}" = "$sub" ]; then
      echo "fixture tmux: failing '$sub' on request" >&2
      exit 1
    fi
    printf '%s\n' "$*" >> "${FIXTURE_TMUX_LOG:-/dev/null}"
    state="${FIXTURE_TMUX_STATE:-}"
    case "$sub" in
      new-session)
        cwd="" cmd=""
        shift
        while [ $# -gt 0 ]; do
          case "$1" in
            -c) cwd="$2"; shift 2 ;;
            -s|-t) shift 2 ;;
            -d) shift ;;
            \;) break ;;
            *) cmd="$1"; shift ;;
          esac
        done
        mkdir -p "$state"
        if [ -n "${FIXTURE_TMUX_SERVER_CONFIG_DIR+set}" ]; then
          if [ -n "$FIXTURE_TMUX_SERVER_CONFIG_DIR" ]; then
            export CLAUDE_CONFIG_DIR="$FIXTURE_TMUX_SERVER_CONFIG_DIR"
          else
            unset CLAUDE_CONFIG_DIR
          fi
        fi
        (cd "$cwd" && exec /bin/sh -c '/bin/sh -c "$1"; echo $? > "$2"' sh "$cmd" "$state/status") \
          </dev/null >"$state/pane" 2>&1 &
        echo $! > "$state/pid" ;;
      display-message)
        # Expands the format as tmux does: a shell's status above 128 is a death by signal
        # 128 less, which tmux reports as the signal with no status.
        fmt="${!#}" dead=0 code="" sig=""
        if [ -e "$state/status" ]; then
          dead=1 code=$(cat "$state/status")
          if [ "$code" -gt 128 ]; then sig=$((code - 128)) code=""; fi
        fi
        line="${fmt//\#\{pane_dead\}/$dead}"
        line="${line//\#\{pane_dead_status\}/$code}"
        line="${line//\#\{pane_dead_signal\}/$sig}"
        case "$line" in
          *'#{'*) echo "fixture tmux: cannot expand '$fmt'" >&2; exit 2 ;;
        esac
        printf '%s\n' "$line" ;;
      load-buffer)
        if [ "${!#}" = - ]; then
          printf 'buffer: %s\n' "$(cat)" >> "${FIXTURE_TMUX_LOG:-/dev/null}"
        fi ;;
      capture-pane)
        if [ -n "${FIXTURE_TMUX_PANE_TEXT+set}" ]; then
          printf '%s\n' "$FIXTURE_TMUX_PANE_TEXT"
        else
          cat "$state/pane"
        fi ;;
      kill-session)
        pid=$(cat "$state/pid" 2>/dev/null) || exit 1
        pkill -KILL -P "$pid" 2>/dev/null; kill -KILL "$pid" 2>/dev/null
        rm -f "$state/pid" ;;
    esac
    exit 0
    ;;
  *) echo "fixture tmux: unexpected subcommand ${1:-}" >&2; exit 2 ;;
esac
"""

NEST = r"""#!/bin/bash
# nest DEPTH CMD... - put DEPTH+1 real processes above CMD, so CMD sits at
# ancestry distance DEPTH+1 from the first one. That first process publishes its
# pid to $NEST_PUBLISH_PID; the pane fixtures are written against it.
set -uo pipefail
depth="$1"; shift
# A planted claude's own arguments follow its depth (see below); the claude drops them here.
if [ -n "${NEST_CLAUDE_SKIP:-}" ]; then shift "$NEST_CLAUDE_SKIP"; unset NEST_CLAUDE_SKIP; fi
self="${NEST_SELF:-$0}"
if [ -n "${NEST_PUBLISH_PID:-}" ]; then
  printf '%s' "$$" > "$NEST_PUBLISH_PID"
  unset NEST_PUBLISH_PID
fi
# The hop that runs as a claude, planted so the launcher's ancestry walk has a
# claude process of this fixture's own making to find. Re-exec rather than fork: the
# pid is unchanged, so the pid already published above IS this claude - which is
# what lets an assertion name the exact process the launcher was supposed to find.
# The walk knows a claude by the name of the executable it runs, so the hop execs a
# copy of bash named as $NEST_CLAUDE_BIN names it (see `exe`). Everything below
# this hop proceeds as ordinary nest.
# $NEST_CLAUDE_AT may name several depths; each is planted once. The pid of the
# last one planted (the deepest) goes to $NEST_RECORD_CLAUDE, so a case can name
# the exact claude it expects the launcher to pick, and with $NEST_EXPORT_CLAUDE_PID
# it is exported as $CLAUDE_PID, what Claude Code hands every tool call.
# $NEST_CLAUDE_ARGV, one argument a line, is in every planted claude's argv after its
# depth, as a claude `claude remote-control` spawned holds its session's URL there.
case " ${NEST_CLAUDE_AT:-} " in
  *" $depth "*)
    NEST_CLAUDE_AT=" $NEST_CLAUDE_AT "; NEST_CLAUDE_AT="${NEST_CLAUDE_AT// $depth / }"
    export NEST_CLAUDE_AT
    [ -n "${NEST_RECORD_CLAUDE:-}" ] && printf '%s' "$$" > "$NEST_RECORD_CLAUDE"
    [ -n "${NEST_EXPORT_CLAUDE_PID:-}" ] && export CLAUDE_PID=$$
    own=()
    [ -n "${NEST_CLAUDE_ARGV:-}" ] && while IFS= read -r word; do own+=("$word"); done <<<"$NEST_CLAUDE_ARGV"
    export NEST_CLAUDE_SKIP=${#own[@]}
    exec "$NEST_CLAUDE_BIN" "$self" "$depth" ${own[@]+"${own[@]}"} "$@" ;;
esac
if [ "$depth" -gt 0 ]; then
  if [ "${NEST_REHOST_AT:-}" = "$depth" ]; then
    unset NEST_REHOST_AT
    # A host hop: the claude shim stands in for the host, a daemon by default, run by
    # the executable $NEST_HOST_BIN names, a claude by default, so a case can plant a
    # host whose executable is named tmux instead. Its argv after the executable is
    # $NEST_HOST_ARGV, one argument a line, then `--` and the rest of the chain. bash
    # runs the first argument as its script, found in $NEST_VERBS where it names the
    # shim, so a host's argv can open with a subcommand, as `claude remote-control`'s
    # does, or with a node install's script.
    host=()
    while IFS= read -r word; do host+=("$word"); done <<<"$NEST_HOST_ARGV"
    (export NEST_CWD="$PWD"; cd "$NEST_VERBS" && exec "$NEST_HOST_BIN" "${host[@]}" -- "$self" $((depth - 1)) "$@")
    exit $?
  fi
  "$self" $((depth - 1)) "$@"
  status=$?
  # $NEST_LINGER keeps the planted claude alive after the launcher returns, as a claude
  # outlives its tool call, so a worker has a live old process to retire.
  [ -n "${NEST_LINGER:-}" ] && [ "${CLAUDE_PID:-}" = "$$" ] \
    && sleep "$NEST_LINGER" </dev/null >/dev/null 2>&1
  exit $status
fi
# $NEST_SLEEP holds the bottom of the chain still long enough that ps reports a
# non-zero elapsed time for it. Without that the whole chain is born inside one
# second, every age reads 00:00, and no forged age can be younger than its own
# descendant - so the age check would have nothing to catch.
[ -n "${NEST_SLEEP:-}" ] && sleep "$NEST_SLEEP"
"$@"
exit $?
"""

# The daemon's command line, which both fixture claudes answer, since the launcher runs
# whichever claude is on PATH. `agents --json` prints $FIXTURE_AGENTS, `[]` (no session
# is a background session) by default, and exits $FIXTURE_AGENTS_STATUS. `stop <id>`
# appends its argv to $FIXTURE_STOP_LOG; for the id $FIXTURE_BACKGROUND_ID it ends the
# process whose pid is in the file $FIXTURE_BACKGROUND_PID with SIGUSR1, a signal only
# this fixture sends, so a case tells the daemon's stop from the worker's TERM, unless
# $FIXTURE_STOP_LEAVES says the stop is accepted and the process stays. Any other id is
# refused as 2.1.299 refuses one.
DAEMON_CLI = r"""
case "${1:-}" in
  agents)
    printf '%s\n' "${FIXTURE_AGENTS:-[]}"
    exit "${FIXTURE_AGENTS_STATUS:-0}" ;;
  stop)
    printf '%s\n' "$*" >> "${FIXTURE_STOP_LOG:-/dev/null}"
    if [ -n "${FIXTURE_BACKGROUND_ID:-}" ] && [ "${2:-}" = "$FIXTURE_BACKGROUND_ID" ]; then
      [ -n "${FIXTURE_STOP_LEAVES:-}" ] || kill -USR1 "$(cat "$FIXTURE_BACKGROUND_PID")"
      echo "Stopped $2"
      exit 0
    fi
    echo "No job matching $2" >&2
    exit 1 ;;
esac
"""

CLAUDE = r"""#!/bin/bash
# Stands in for `claude daemon run --bg-pty-host`: the daemon host. A session it
# hosts is not displayed by any pane above it, and by default inherits none of
# tmux's environment - stripped here for real, not simulated.
set -uo pipefail
""" + DAEMON_CLI + r"""
# The host hop ran this from the directory that names it; the chain goes on where it was.
cd "$NEST_CWD" || exit 2
unset NEST_CWD
while [ "${1:-}" != "--" ]; do
  [ $# -gt 0 ] || { echo "claude shim: no -- separator" >&2; exit 2; }
  shift
done
shift
# $FIXTURE_HOST_KEEPS_TMUX: a daemon whose spawner sat in a pane passes that
# pane's $TMUX_PANE down, the leak a host boundary has to survive.
[ -n "${FIXTURE_HOST_KEEPS_TMUX:-}" ] && { "$@"; exit $?; }
env -u TMUX -u TMUX_PANE "$@"
exit $?
"""

PS = r"""#!/bin/bash
# The real ps, with the forgeries a case asks for written into the tree the
# launcher's walk reads (`ps -eo pid=,ppid=,etime=,ucomm=`).
# Each forgery is a value in the environment, applied to a row the case names, so
# every case runs this same wrapper and differs only in what it writes:
#   $FIXTURE_FORGE_AGE      the chain root's elapsed time. A pid younger than the
#                           descendant claiming it is the signature of a reissued
#                           pid, and the one thing no live machine produces on cue.
#                           An older age is the control: the same rewrite of the
#                           same row, which must still resolve.
#   $FIXTURE_RUNNER_PID     the suite runner, whose parent reads as pid 1. Without
#                           it, a walk that meets no pane climbs past the runner into
#                           whatever session launched the suite. A host a case plants
#                           sits below the runner and is still seen.
#   $FIXTURE_PS_FAILS       the whole table read exits 1.
#   $FIXTURE_MUTE_IDENTITY  the identity read (`-o lstart=,ucomm= -p <pid>`)
#                           exits 0 printing nothing, which is what a pid whose
#                           holder exited reports: success with no identity in it.
# Every other ps call passes straight through.
set -uo pipefail
for arg in "$@"; do
  [ "$arg" = "lstart=,ucomm=" ] && [ -n "${FIXTURE_MUTE_IDENTITY:-}" ] && exit 0
done
[ "${1:-}" = "-eo" ] || exec /bin/ps "$@"
[ -z "${FIXTURE_PS_FAILS:-}" ] || { echo "ps: fixture refuses the process table" >&2; exit 1; }
/bin/ps "$@" | /usr/bin/awk -v root="$(cat "$FIXTURE_ROOT_PID")" -v runner="${FIXTURE_RUNNER_PID:-}" \
    -v age="${FIXTURE_FORGE_AGE:-}" '
  {
    if ($1 == runner) $2 = 1
    if ($1 == root && age != "") $3 = age
    print
  }'
"""


def install(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write(body)
    os.chmod(path, 0o755)
    return path


SUCCESSOR = r"""#!/bin/bash
# The successor a relaunch starts: takes --session-id and the prompt (its last
# argument), and does what $FIXTURE_SUCCESSOR says, which is every way a successor
# can answer:
#   records  writes the prompt as a user record into its own transcript under the
#            config dir its environment says, as claude does when it takes one
#   late     the same, after FIXTURE_SUCCESSOR_DELAY seconds
#   other    records a prompt that is not the one it was given
#   silent   records nothing
#   dies     prints why on its terminal and exits 3 at boot
#   killed   is killed by SIGTERM at boot
# It then holds its pid, as a running claude does, until it is killed.
set -uo pipefail
""" + DAEMON_CLI + r"""
id="" prompt=""
while [ $# -gt 0 ]; do
  case "$1" in
    --session-id) id="$2"; shift 2 ;;
    *) prompt="$1"; shift ;;
  esac
done
config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
record() {
  mkdir -p "$config/projects/-fixture"
  /usr/bin/python3 -I -c 'import json, sys; print(json.dumps({"type": "user", "message": {"role": "user", "content": sys.argv[1]}}))' \
    "$1" >> "$config/projects/-fixture/$id.jsonl"
}
case "${FIXTURE_SUCCESSOR:-records}" in
  records) record "$prompt" ;;
  late) sleep "${FIXTURE_SUCCESSOR_DELAY:-3}"; record "$prompt" ;;
  other) record "a prompt that is not this handoff" ;;
  silent) ;;
  dies) echo "error: unknown option '--bogus'"; exit 3 ;;
  killed) kill -TERM $$ ;;
esac
echo "fixture successor $id is up"
exec sleep 120
"""

FIXTURES = tempfile.mkdtemp(prefix="finalize-fixtures.")
install(FIXTURES, "tmux", TMUX)
HOST_SHIM = install(FIXTURES, "claude", CLAUDE)
NEST_BIN = install(FIXTURES, "nest", NEST)
PS_DIR = os.path.join(FIXTURES, "ps")
os.mkdir(PS_DIR)
install(PS_DIR, "ps", PS)
# The iTerm2 transport's probe asks osascript for iTerm2's version, so an osascript that
# answers stands in for a running iTerm2. It goes on PATH only for an iTerm2 case. Every
# other script, a write to a session, comes on stdin: it is appended to
# $FIXTURE_OSASCRIPT_LOG and exits $FIXTURE_OSASCRIPT_STATUS.
ITERM_BIN = os.path.join(FIXTURES, "iterm")
os.mkdir(ITERM_BIN)
install(ITERM_BIN, "osascript", """#!/bin/bash
if [ "${1:-}" = -e ]; then echo 3.6.0; exit 0; fi
cat >> "${FIXTURE_OSASCRIPT_LOG:-/dev/null}"
exit "${FIXTURE_OSASCRIPT_STATUS:-0}"
""")
# The successor's claude, named claude so `command -v claude` in a launcher finds it.
SUCCESSOR_DIR = os.path.join(FIXTURES, "successor")
os.mkdir(SUCCESSOR_DIR)
SUCCESSOR_BIN = install(SUCCESSOR_DIR, "claude", SUCCESSOR)

# A Remote Control session's login and session server. `security` answers the keychain read with a
# login, so the poster finds one on macOS, and the same login is written as .credentials.json where it
# looks elsewhere. The session server is the shared fake (lib/fake_session_server.py), whose session id
# chooses how the session behaves, so each case differs only in the id it hands the launcher.
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "lib"))
import fake_session_server  # noqa: E402
import remote_control  # noqa: E402

KEYCHAIN_DIR = os.path.join(FIXTURES, "keychain")
os.mkdir(KEYCHAIN_DIR)
LOGIN = '{"claudeAiOauth": {"accessToken": "%s"}}' % fake_session_server.TOKEN
install(KEYCHAIN_DIR, "security", f"#!/bin/bash\nprintf '%s' '{LOGIN}'\n")
SESSION_SERVER = fake_session_server.serve()
# A host's argv after its executable: what `claude daemon run` and `claude remote-control` run with.
DAEMON_ARGV = ["claude", "daemon", "run", "--bg-pty-host"]
SERVER_ARGV = ["remote-control", "--spawn", "worktree", "--name", "memento"]


def listed_background(id, session_id):
    """A background session's entry in `claude agents --json`, as 2.1.299 lists one: no pid."""
    return {"id": id, "cwd": "/tmp", "kind": "background", "startedAt": 1794394494195,
            "sessionId": session_id, "name": "a job", "state": "working"}


def session_url(id):
    """The URL a remote-control server starts a session's claude with, on the fake session server."""
    return f"{SESSION_SERVER}/v1/code/sessions/{id}"


def spawned_argv(id):
    """The arguments a remote-control server starts a session's claude with, as 2.1.299's does."""
    return ["--print", "--sdk-url", session_url(id), "--session-id", id, "--input-format", "stream-json",
            f"--resume={session_url(id)}"]


def exe(name):
    """A bash whose executable is named `name`, which is how the walk knows a claude or a tmux: the
    kernel records the name of the file executed. A symlink is recorded under its target's name, and
    a copy of a system binary runs only once it is signed again."""
    path = os.path.join(FIXTURES, "exe", name, name)
    os.makedirs(os.path.dirname(path))
    shutil.copy("/bin/bash", path)
    subprocess.run(["codesign", "-f", "-s", "-", path], check=True, capture_output=True)
    return path


# Each name a claude runs as: the launcher symlink's, the native installer's versioned binary,
# nix's wrapper, and an npm install's node.
CLAUDE_BIN, VERSIONED_CLAUDE_BIN, WRAPPED_CLAUDE_BIN, NODE_BIN = (
    exe(name) for name in ("claude", "2.1.299", ".claude-wrapped", "node"))
TMUX_SERVER_BIN, WRAPPED_TMUX_BIN = exe("tmux"), exe(".tmux-wrapped")
# Not a claude: a name only one dot short of a version, and a shell.
NOT_CLAUDE_BIN, ZSH_BIN = exe("2.1"), exe("zsh")

SESSION_ID = "6a1f9e2c-3b7d-4e8a-9c0f-5d2b1a7e3c94"
# The id the fixture daemon's listing gives a background session, as `claude --bg` prints one.
BACKGROUND_ID = "b08daab6"
# A session id as the launcher mints one for a successor: a lowercase UUID4.
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


def prompt(mode):
    return {"type": "user", "permissionMode": mode, "message": {"role": "user", "content": "go"}}


def state(mode):
    return {"type": "permission-mode", "permissionMode": mode, "sessionId": SESSION_ID}


def answer(effort):
    record = {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5"}}
    if effort is not None:
        record["effort"] = effort
    return record


# What a session in bypassPermissions at high effort records.
BYPASS_HIGH = [prompt("bypassPermissions"), answer("high")]


def write_transcripts(config_dir, transcripts):
    """One transcript per entry, each a list of records, in its own project slug, as Claude Code
    keeps them under `<config dir>/projects/<slug>/<session id>.jsonl`."""
    for slug, records in enumerate(transcripts):
        project = os.path.join(config_dir, "projects", f"-case-{slug}")
        os.makedirs(project)
        with open(os.path.join(project, SESSION_ID + ".jsonl"), "w") as handle:
            handle.writelines(json.dumps(record) + "\n" for record in records)

# A `readlink` that removes a directory on its first call, then behaves normally.
# This makes the launcher's own directory disappear mid-run at a fixed point: the
# launcher resolves its own path through symlinks with `readlink -f` first, so the
# removal lands just before the path it reads. It models a script whose directory is
# unlinked under a running session.
RMDIR_READLINK = r"""#!/bin/bash
set -uo pipefail
if [ -n "${FIXTURE_RM_DIR:-}" ] && [ -e "$FIXTURE_RM_DIR" ]; then
  rm -rf -- "$FIXTURE_RM_DIR"
fi
exec %s "$@"
""" % shutil.which("readlink")
RMDIR_READLINK_DIR = os.path.join(FIXTURES, "rmdirreadlink")
os.mkdir(RMDIR_READLINK_DIR)
install(RMDIR_READLINK_DIR, "readlink", RMDIR_READLINK)
# A mktemp that refuses, so the launcher sees a tmpdir with no room in it.
NO_TMPDIR_BIN = os.path.join(FIXTURES, "notmpdir")
os.mkdir(NO_TMPDIR_BIN)
install(NO_TMPDIR_BIN, "mktemp", '#!/bin/bash\necho "mktemp: no space left on device" >&2\nexit 1\n')

# A mktemp that refuses only for the log tempfile. The blanket refusal above also
# blocks the goal tempfile, so the launcher dies before the handoff is written. This
# narrower version lets the handoff land first.
NO_LOGFILE_BIN = os.path.join(FIXTURES, "nologfile")
os.mkdir(NO_LOGFILE_BIN)
# `mktemp` by absolute path, not by name: this directory goes on the front of
# PATH, so a bare `mktemp` would find this script again.
install(NO_LOGFILE_BIN, "mktemp", '''#!/bin/bash
for arg in "$@"; do
  case "$arg" in
    */finalize-log.*) echo "mktemp: no space left on device" >&2; exit 1 ;;
  esac
done
exec /usr/bin/mktemp "$@"
''')

# A mktemp that succeeds but returns an unwritable path, for the goal tempfile
# only. The write that must fail is the goal write, not the mktemp before it, so
# the handoff is already on disk when the refusal happens.
BAD_GOALPATH_BIN = os.path.join(FIXTURES, "badgoalpath")
os.mkdir(BAD_GOALPATH_BIN)
install(BAD_GOALPATH_BIN, "mktemp", '''#!/bin/bash
for arg in "$@"; do
  case "$arg" in
    */finalize-goal.*) echo "/nonexistent-finalize-dir/goal"; exit 0 ;;
  esac
done
exec /usr/bin/mktemp "$@"
''')
# A python3 that makes the launcher's goal tempfile read-only as the launcher claims a published goal, so
# the one step after the claim, writing the claimed goal, fails.
NO_GOAL_REWRITE_BIN = os.path.join(FIXTURES, "nogoalrewrite")
os.mkdir(NO_GOAL_REWRITE_BIN)
install(NO_GOAL_REWRITE_BIN, "python3", '''#!/bin/bash
[ "${3:-}" = claim ] && chmod 444 "${TMPDIR:-/tmp}"/finalize-goal.*
exec /usr/bin/python3 "$@"
''')

# A launcher whose working directory no longer exists, as after a removed worktree.
# The hop cds into a directory and removes it, so the kernel has no path for the cwd
# it hands the launcher, and `pwd -P` fails. The case asserts only the exit contract,
# so any other unguarded failure on this path is caught too.
CWDGONE = r"""#!/bin/bash
set -uo pipefail
d=$(mktemp -d "${TMPDIR:-/tmp}/cwdgone.XXXXXX") || exit 1
cd "$d" || exit 1
rmdir "$d" || exit 1
exec "$@"
"""
CWDGONE_BIN = install(FIXTURES, "cwdgone", CWDGONE)

# A PATH with everything the launcher needs and no tmux. The absence is built, not
# observed, because the real directories may or may not hold tmux depending on the
# host. The tmux-absent case runs with FIXTURES off PATH; elsewhere FIXTURES comes
# first and the fixture tmux shadows any real one.
# It mirrors REAL_DIRS entry for entry, so it cannot go stale as the launcher's
# required tools change.
NO_TMUX_BIN = os.path.join(FIXTURES, "notmux")
os.mkdir(NO_TMUX_BIN)
for directory in REAL_DIRS.split(":"):
    for entry in sorted(os.listdir(directory)):
        link = os.path.join(NO_TMUX_BIN, entry)
        # Skipping an existing link keeps REAL_DIRS' own precedence, the way a
        # PATH search would resolve it.
        if entry != "tmux" and not os.path.lexists(link):
            os.symlink(os.path.join(directory, entry), link)

# A live process that is not an ancestor of the launcher. The runner is an ancestor
# of every `nest` chain, so a child of the runner is a sibling of the chain.
STRANGER = subprocess.Popen(["sleep", "600"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def descendants(root):
    """Every live process below `root`, found by walking parent pids, which is how Claude Code finds
    the tree of a Bash call it stops."""
    rows = subprocess.run(["/bin/ps", "-eo", "pid=,ppid="], capture_output=True, text=True).stdout
    children = {}
    for row in rows.split("\n"):
        if row.strip():
            pid, ppid = row.split()
            children.setdefault(int(ppid), []).append(int(pid))
    found, frontier = [], [root]
    while frontier:
        below = children.get(frontier.pop(), [])
        found += below
        frontier += below
    return found


def wait_for(condition, seconds):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        time.sleep(0.1)
    return condition()


def run_killing_launcher_tree(argv, env, tmux_state):
    """Run the chain, and once the successor exists, kill everything below the planted claude: the
    launcher's Bash call, stopped mid-wait. The planted claude itself is the old session, and lives."""
    chain = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    wait_for(lambda: os.path.exists(os.path.join(tmux_state, "pid")), 30)
    for pid in descendants(chain.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    out, err = chain.communicate(timeout=120)
    return subprocess.CompletedProcess(argv, chain.returncode, out, err)


def settle_detached(done, workdir, tmux_state):
    """After a real detached run: whether the planted claude was retired (it ends on the worker's
    TERM, or KILL, instead of outliving its linger), the tmux calls, and the worker's log, read once
    the worker has exited. The successor is then killed, so no case leaves one behind."""
    done.retired = done.returncode in (-signal.SIGTERM, -signal.SIGKILL)

    def worker_running():
        rows = subprocess.run(["/bin/ps", "-eo", "args="], capture_output=True, text=True).stdout
        return any("--detached-worker" in row and workdir in row for row in rows.split("\n"))

    wait_for(lambda: not worker_running(), 45)
    tmuxlog = os.path.join(workdir, "tmux.log")
    done.tmux_log = open(tmuxlog).read() if os.path.exists(tmuxlog) else ""
    stoplog = os.path.join(workdir, "stop.log")
    done.stop_log = open(stoplog).read() if os.path.exists(stoplog) else ""
    pane = os.path.join(tmux_state, "pane")
    done.pane = open(pane).read() if os.path.exists(pane) else ""
    logged = done.stdout.rpartition("(log: ")[2].rpartition(")")[0]
    done.worker_log = open(logged).read() if logged and os.path.exists(logged) else ""
    try:
        with open(os.path.join(tmux_state, "pid")) as handle:
            successor = handle.read().strip()
        subprocess.run(["pkill", "-KILL", "-P", successor], capture_output=True)
        subprocess.run(["kill", "-KILL", successor], capture_output=True)
    except FileNotFoundError:
        pass


def settle_remote(done, url):
    """After a real remote-control run: the remote worker's log, read once the worker has exited, so the
    case sees every post it made. The worker is known by the session it posts to, which each case names
    uniquely."""
    def worker_running():
        rows = subprocess.run(["/bin/ps", "-eo", "args="], capture_output=True, text=True).stdout
        return any(f"--remote-worker {url} " in row for row in rows.split("\n"))

    wait_for(lambda: not worker_running(), 30)
    logged = done.stdout.rpartition("(log: ")[2].rpartition(")")[0]
    done.worker_log = open(logged).read() if logged and os.path.exists(logged) else ""


def summarize(journal, text, session_id=SESSION_ID):
    """Records a summary in `journal` as an agent does ahead of the launcher: written to a file, and
    given to the journal's command by path. A `session_id` of None records it with no session."""
    env = {"PATH": REAL_DIRS, "MEMENTO_JOURNAL": journal}
    if session_id is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    with tempfile.NamedTemporaryFile("w", suffix=".md") as file:
        file.write(text)
        file.flush()
        subprocess.run([JOURNAL_CLI, "summary", "--file", file.name], env=env, check=True,
                       capture_output=True)


def run(depth=1, panes="%99 ROOTPID 0", tmux_on_path=True, forge_age=None,
        rehost_at=None, tmux_pane=None, sleep=None, mute_identity=False,
        message="handoff", handoff_dir=None, journal=None, summary="handed off", dry_run="1",
        log_mktemp_fails=False, goal_rewrite_fails=False, cwd_gone=False, ps_fails=False,
        self_dir_gone=False, via_link=False, claude_bin=CLAUDE_BIN, claude_pid=None,
        claude_at=None, host_keeps_tmux=False, outer=None, outer_is_own=False,
        launcher_flags=(), host_bin=CLAUDE_BIN, export_claude_pid=True,
        transcripts=(BYPASS_HIGH,), session_id=SESSION_ID, entrypoint="cli", iterm=False,
        successor=None, server_config_dir=None, config_unset=False, kill_launcher_tree=False,
        bridge=None, host_argv=None, claude_argv=None, agents=None, agents_status=None, tmux_fail=None,
        osascript_status=None):
    """Launch finalize-session under a real `nest` chain and return what it reported.

    `dry_run` is the raw value of $FINALIZE_DRY_RUN; the empty string means off,
    so the real transport path runs the same code as the dry-run cases.
    Every run has a journal of its own, in its workdir unless `journal` names one to
    read back. A real run closes out with the summary its session recorded, so one
    holding `summary` is recorded first; None records none.
    `claude_bin` is the executable the planted claude runs as, and `host_bin` the
    one a host hop runs as. The planted claude's pid is $CLAUDE_PID unless
    `export_claude_pid` is off; `claude_pid` sets $CLAUDE_PID to a value instead.
    `outer` is an executable wrapped around the whole chain, and `outer_is_own`
    makes it $CLAUDE_PID. `transcripts` are written under the case's own config
    directory, and `session_id` and `entrypoint` are the session's environment;
    None leaves either unset. `iterm` runs the session in a reachable iTerm2.

    `successor` runs the detached transport for real (dry run off), with the fixture
    successor answering as `successor` names (see SUCCESSOR) and the planted claude
    outliving the launcher, as a claude outlives its tool call. `done.retired` says
    whether the worker then retired it, `done.tmux_log` lists the tmux calls, and
    `done.worker_log` is the worker's log when the launcher named it, and `done.pane` what
    the successor's pane showed. The successor has READY_TIMEOUT seconds to take the message.
    `server_config_dir` is the tmux server's own CLAUDE_CONFIG_DIR ("" for unset), and
    `config_unset` leaves the session's unset, so its config is $HOME/.claude under a
    HOME of the case's own. `kill_launcher_tree` kills the launcher and everything
    below it once the successor exists, as Claude Code kills a stopped Bash call.

    `host_argv` is a host hop's argv after its executable, a daemon's by default, and `claude_argv` the
    arguments every planted claude holds. `bridge` is a Remote Control session id: the host is a
    `claude remote-control` server and the session's claude holds the arguments it starts one with,
    unless the case names others; the environment is the one Claude Code gives such a session; and the
    login and session server fixtures stand in for the keychain and the server. With the dry run off, the
    remote worker the launcher starts is waited out before the case ends, and `done.worker_log` is its log.

    The session listing (`claude agents --json`) names the session as background session BACKGROUND_ID,
    and the fixture daemon's `claude stop` of that id ends the deepest planted claude: `done.stop_log`
    lists the stops asked for. `agents` is the listing's text instead, and `agents_status` the exit
    status of `claude agents --json`.

    Every case's `done.tmux_log` lists the tmux calls made, and `tmux_fail` names one tmux subcommand to
    fail. A real run on the tmux transport schedules the tmux worker, which acts only after its delay, so
    `done.tmux_log` is the launcher's own calls, and the worker is killed before the case ends.
    `done.osascript_log` is the AppleScript the launcher wrote to iTerm2 sessions, and `osascript_status`
    the status those writes exit with. A real run on the iTerm2 transport has its worker killed the same way.
    """
    workdir = tempfile.mkdtemp(prefix="finalize-case.")
    if host_argv is None:
        host_argv = DAEMON_ARGV if bridge is None else SERVER_ARGV
    if claude_argv is None:
        claude_argv = [] if bridge is None else spawned_argv(bridge)
    verbs = os.path.join(workdir, "verbs")
    os.mkdir(verbs)
    os.symlink(HOST_SHIM, os.path.join(verbs, host_argv[0]))
    pidfile = os.path.join(workdir, "root.pid")
    home = os.path.join(workdir, "home") if config_unset else HOME
    config_dir = os.path.join(home, ".claude") if config_unset else os.path.join(workdir, "config")
    write_transcripts(config_dir, transcripts)
    tmux_state = os.path.join(workdir, "tmux")
    # The tmux-absent case uses NO_TMUX_BIN in place of FIXTURES and REAL_DIRS: it must
    # not include REAL_DIRS, since that is where a packaged tmux would be found.
    # The ps fixture sits first on every case's PATH; a case changes only the
    # forgeries it writes.
    path = [PS_DIR] + ([FIXTURES, REAL_DIRS] if tmux_on_path else [NO_TMUX_BIN])
    if log_mktemp_fails:
        path.insert(0, NO_LOGFILE_BIN)
    if goal_rewrite_fails:
        path.insert(0, NO_GOAL_REWRITE_BIN)
    # The launcher is invoked through a symlinked directory that the fixture `date`
    # deletes on its first call. The symlink is what disappears, not the script file,
    # so the launcher keeps running as it would in the field.
    selfdir = None
    if self_dir_gone or via_link:
        selfdir = os.path.join(workdir, "selfdir")
        os.mkdir(selfdir)
        os.symlink(LAUNCHER, os.path.join(selfdir, "finalize-session"))
    if self_dir_gone:
        path.insert(0, RMDIR_READLINK_DIR)
    if successor is not None:
        path.insert(0, SUCCESSOR_DIR)
    # Built from scratch, so nothing of the session running the suite (its $CLAUDE_PID,
    # its session id, its config directory) reaches the case.
    env = {
        "PATH": ":".join(path),
        "HOME": home,
        "TMPDIR": workdir,
        "FINALIZE_DRY_RUN": "" if successor is not None else dry_run,
        "NEST_PUBLISH_PID": pidfile,
        "FIXTURE_ROOT_PID": pidfile,
        "FIXTURE_RUNNER_PID": str(os.getpid()),
        # The claude process the relaunching transports retire is planted at the top of
        # this chain, so the ancestry walk finds a process this fixture owns rather than
        # whatever real claude happens to be running the suite.
        "NEST_CLAUDE_AT": " ".join(str(d) for d in (
            [depth] if claude_at is None else claude_at if isinstance(claude_at, list) else [claude_at])),
        "NEST_RECORD_CLAUDE": os.path.join(workdir, "claude.pid"),
        "NEST_CLAUDE_BIN": claude_bin,
        "NEST_HOST_BIN": host_bin,
        "NEST_HOST_ARGV": "\n".join(host_argv),
        "NEST_VERBS": verbs,
        "NEST_CLAUDE_ARGV": "\n".join(claude_argv),
    }
    if bridge is not None:
        env["PATH"] = KEYCHAIN_DIR + ":" + env["PATH"]
        env.update(CLAUDE_CODE_ENVIRONMENT_KIND="bridge", MEMENTO_RESET_TIMEOUT_SECONDS="2",
                   MEMENTO_IDLE_TIMEOUT_SECONDS="2")
        if not config_unset:
            os.makedirs(config_dir, exist_ok=True)
            with open(os.path.join(config_dir, ".credentials.json"), "w") as handle:
                handle.write(LOGIN)
    if not config_unset:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    tmuxlog = os.path.join(workdir, "tmux.log")
    env["FIXTURE_TMUX_LOG"] = tmuxlog
    if tmux_fail is not None:
        env["FIXTURE_TMUX_FAIL"] = tmux_fail
    osascriptlog = os.path.join(workdir, "osascript.log")
    env["FIXTURE_OSASCRIPT_LOG"] = osascriptlog
    if osascript_status is not None:
        env["FIXTURE_OSASCRIPT_STATUS"] = str(osascript_status)
    if successor is not None:
        env.update(FIXTURE_SUCCESSOR=successor, FIXTURE_TMUX_STATE=tmux_state, NEST_LINGER="20",
                   MEMENTO_READY_TIMEOUT_SECONDS=str(READY_TIMEOUT))
    if server_config_dir is not None:
        env["FIXTURE_TMUX_SERVER_CONFIG_DIR"] = server_config_dir
    env.update(FIXTURE_BACKGROUND_ID=BACKGROUND_ID, FIXTURE_BACKGROUND_PID=env["NEST_RECORD_CLAUDE"],
               FIXTURE_STOP_LOG=os.path.join(workdir, "stop.log"),
               FIXTURE_AGENTS=json.dumps([listed_background(BACKGROUND_ID, session_id)]) if agents is None else agents)
    if agents_status is not None:
        env["FIXTURE_AGENTS_STATUS"] = str(agents_status)
    if iterm:
        env["PATH"] = ITERM_BIN + ":" + env["PATH"]
        env["TERM_PROGRAM"] = "iTerm.app"
        env["ITERM_SESSION_ID"] = "w0t0p0:5C2E7B1A-0D4F-4E8B-9A3C-6F1D2E7B8A90"
    if session_id is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    if entrypoint is not None:
        env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
    if claude_pid is not None:
        env["CLAUDE_PID"] = str(claude_pid)
    # Set from inside the chain: the planted claude's pid exists only once it runs.
    elif export_claude_pid:
        env["NEST_EXPORT_CLAUDE_PID"] = "1"
    if host_keeps_tmux:
        env["FIXTURE_HOST_KEEPS_TMUX"] = "1"
    if ps_fails:
        env["FIXTURE_PS_FAILS"] = "1"
    if mute_identity:
        env["FIXTURE_MUTE_IDENTITY"] = "1"
    if self_dir_gone:
        env["FIXTURE_RM_DIR"] = selfdir
    if forge_age is not None:
        env["FIXTURE_FORGE_AGE"] = forge_age
    if panes is not None:
        env["FIXTURE_PANES"] = panes
    if rehost_at is not None:
        env["NEST_REHOST_AT"] = str(rehost_at)
    if tmux_pane is not None:
        env["TMUX_PANE"] = tmux_pane
    # Every case must redirect the handoff directory, because handoffs are durable
    # and an unredirected run would leave files in the real ~/.claude/memento/handoffs.
    # The default is inside this case's workdir, which is removed; a case that reads
    # the handoff back passes its own directory.
    env["MEMENTO_HANDOFF_DIR"] = handoff_dir or os.path.join(workdir, "handoffs")
    # The journal is durable too, and an unredirected real run would write the user's.
    env["MEMENTO_JOURNAL"] = journal or os.path.join(workdir, "journal.jsonl")
    if summary is not None and not env["FINALIZE_DRY_RUN"]:
        summarize(env["MEMENTO_JOURNAL"], summary, session_id)
    if sleep is not None:
        env["NEST_SLEEP"] = str(sleep)
    try:
        # The cwd-removing hop sits between the chain and the launcher. The ancestry
        # is unchanged; only the launcher's own cwd is gone.
        launcher = LAUNCHER if selfdir is None else os.path.join(selfdir, "finalize-session")
        entry = [CWDGONE_BIN, launcher] if cwd_gone else [launcher]
        argv = [NEST_BIN, str(depth)] + entry + list(launcher_flags) + [message]
        # A process wrapped around the whole chain, running as the executable `outer`
        # names. The trailing `exit` keeps bash from exec-ing the chain in place of it.
        if outer is not None:
            script = ("export CLAUDE_PID=$$; " if outer_is_own else "") + '"$@"; exit $?'
            argv = [outer, "-c", script, "outer"] + argv
        if kill_launcher_tree:
            done = run_killing_launcher_tree(argv, env, tmux_state)
        else:
            done = subprocess.run(argv,
                                  text=True, capture_output=True, env=env, timeout=120)
        if successor is not None:
            settle_detached(done, workdir, tmux_state)
        else:
            done.tmux_log = open(tmuxlog).read() if os.path.exists(tmuxlog) else ""
            done.osascript_log = open(osascriptlog).read() if os.path.exists(osascriptlog) else ""
            # The tmux and iTerm2 workers' arguments hold the handoff's path, in this case's own handoff
            # directory; its dots, as in mkdtemp's prefixes, are bracketed so the extended regex matches
            # them only. A scheduled worker is waited for first, since until its exec its arguments are
            # the launcher's and nothing matches.
            worker_re = f"[-]-(iterm-)?worker .*{env['MEMENTO_HANDOFF_DIR'].replace('.', '[.]')}"
            if re.search(r"handoff scheduled → (tmux|iTerm2) ", done.stdout):
                wait_for(lambda: subprocess.run(["pgrep", "-f", worker_re], capture_output=True).returncode == 0, 10)
            subprocess.run(["pkill", "-KILL", "-f", worker_re], capture_output=True)
        if bridge is not None and not env["FINALIZE_DRY_RUN"]:
            settle_remote(done, session_url(bridge))
        # The chain's top pid, read before the workdir is removed. It is both the pane
        # fixtures' ROOTPID and the planted claude, so assertions can name the exact
        # process the launcher picked.
        with open(pidfile) as handle:
            done.root_pid = handle.read().strip()
        try:
            with open(os.path.join(workdir, "claude.pid")) as handle:
                done.session_claude_pid = handle.read().strip()
        except FileNotFoundError:
            done.session_claude_pid = None
        return done
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


DECLINED = "declined"
DETACHED = "detached"
# Every way the launcher declines to finish shares one exit code, as its usage block
# states: 2 for refused argv, setup, or transport. NO_TRANSPORT_RC is that code under
# the name the pane-resolution cases use.
REFUSED_RC = 2
NO_TRANSPORT_RC = REFUSED_RC


def picked(done):
    """Return the pane id the launcher picked, DETACHED for the fresh-window
    transport, or DECLINED for the no-transport exit code.

    Any other outcome returns a diagnostic string that no assertion matches, so a
    crash cannot read as a decline.
    """
    for line in done.stdout.splitlines():
        if line.startswith("[dry-run] transport=tmux target="):
            return line.split("target=", 1)[1].split(" ", 1)[0]
        if line.startswith("[dry-run] transport=detached "):
            return DETACHED
    if done.returncode == NO_TRANSPORT_RC:
        return DECLINED
    return f"<no decision: rc={done.returncode} out={done.stdout!r} err={done.stderr!r}>"


def ancestry(done):
    """Return the walk's decision as the dry run reports it, its kind and reason."""
    for line in done.stdout.splitlines():
        if line.startswith("[dry-run] ancestry: "):
            return line[len("[dry-run] ancestry: "):]
    return ""


# --- preconditions --------------------------------------------------------
# These confirm the suite tests the intended binary and a tmux-free PATH. Without
# them, a wrong binary or a real tmux would let the suite pass while proving nothing.

check("the launcher under test exists and is executable", os.access(LAUNCHER, os.X_OK), LAUNCHER)
# This checks the constructed directory, not the host: asking whether REAL_DIRS holds
# tmux would fail the whole suite on any host that packages one into /usr/bin.
check("the tmux-absent PATH contains no tmux",
      shutil.which("tmux", path=NO_TMUX_BIN) is None, NO_TMUX_BIN)
check("the tmux-absent PATH can still run the launcher",
      shutil.which("bash", path=NO_TMUX_BIN) is not None,
      "the launcher's `#!/usr/bin/env bash` resolves bash through PATH")

# --- the flag namespace's border ------------------------------------------
# These drive the launcher directly, with no `nest` chain: argv is refused (or the
# usage printed) before any transport is looked for, so the process tree is not under
# test.
#
# Each case asserts what landed in the handoff directory, not only the exit code. A
# refused argv must write nothing.



def argv_case(*args, path_prefix="", session_id=None):
    """Run the launcher with these args against a handoff directory of its own.

    Returns (completed process, list of handoff bodies written). Bodies are returned
    rather than filenames, since the checks concern what a handoff says. The run is
    dry, and `path_prefix` is prepended to the fixture PATH, never substituted for
    it, so a missing transport cannot masquerade as an argv refusal. `session_id` is
    the $CLAUDE_CODE_SESSION_ID the launcher sees, absent by default.
    """
    path = ":".join(p for p in (path_prefix, FIXTURES, REAL_DIRS) if p)
    workdir = tempfile.mkdtemp(prefix="finalize-argv.")
    handoffs = os.path.join(workdir, "handoffs")
    # The pane's process is this runner, and the launcher runs under a claude below
    # it, as it would in a real pane's session. The trailing `exit` keeps bash from
    # exec-ing the launcher in place of the claude process.
    rootfile = os.path.join(workdir, "root.pid")
    with open(rootfile, "w") as handle:
        handle.write(str(os.getpid()))
    under_claude = [CLAUDE_BIN, "-c", 'export CLAUDE_PID=$$; "$@"; exit $?', "wrap"]
    env = {"PATH": path, "HOME": workdir, "TMPDIR": workdir,
           "MEMENTO_HANDOFF_DIR": handoffs, "FINALIZE_DRY_RUN": "1",
           "FIXTURE_ROOT_PID": rootfile, "FIXTURE_PANES": "%99 ROOTPID 0"}
    if session_id is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    try:
        done = subprocess.run(
            [*under_claude, LAUNCHER, *args], text=True, capture_output=True, timeout=30,
            env=env)
        names = sorted(os.listdir(handoffs)) if os.path.isdir(handoffs) else []
        bodies = [open(os.path.join(handoffs, name)).read() for name in names]
        return done, bodies
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


done, bodies = argv_case("--help")
check("--help prints the usage and records nothing",
      done.returncode == 0 and "usage: finalize-session" in done.stdout and bodies == [],
      f"rc={done.returncode} out={done.stdout!r} handoffs={len(bodies)}")

# A mistyped flag looks like an option but is not one. Without the border it would be
# recorded as the handoff and delivered to the next agent as its instructions.
done, bodies = argv_case("--rest", "compact", "real work goes here")
check("a mistyped flag is refused rather than recorded as the handoff",
      done.returncode == REFUSED_RC and "--rest" in done.stderr and bodies == [],
      f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

# Only a two-dash token is a flag, so a single-dash token is message text. `-h` is the
# case a reader would expect to be a flag and is not.
for text in ("-h", "- shipped the parser"):
    done, bodies = argv_case(text)
    check(f"a single-dash token is message text: {text!r}",
          done.returncode == 0 and len(bodies) == 1 and text in bodies[0],
          f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

# No flag spans a line, so a multi-line argument is message text however it starts.
# Handoffs are often multi-line recaps.
recap = "--goal was already passed\nso this line is recap, not argv"
done, bodies = argv_case(recap)
check("a multi-line message is text even when it opens with two dashes",
      done.returncode == 0 and len(bodies) == 1 and recap in bodies[0],
      f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

# The escape hatch for a single-line message that is itself a two-dash word. Without
# it that handoff could not be recorded.
done, bodies = argv_case("--", "--help")
check("-- hands a lone two-dash word through to the handoff",
      done.returncode == 0 and len(bodies) == 1 and "\n--help\n" in bodies[0],
      f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

# A tempfile the launcher cannot create is refused with its own message and the
# contract's exit code, naming the $TMPDIR it asked for the file in. The goal mktemp runs
# before the handoff is written, so nothing is recorded.
done, bodies = argv_case("a real handoff", path_prefix=NO_TMPDIR_BIN)
check("a tempfile it cannot make is refused as the launcher, not as mktemp, naming $TMPDIR",
      done.returncode == REFUSED_RC and "finalize-session:" in done.stderr
      and f"tempfile in {os.path.join(tempfile.gettempdir(), 'finalize-argv.')}" in done.stderr
      and bodies == [],
      f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

# A refusal while creating the log tempfile happens after the handoff is written, so
# the launcher must name the recorded handoff's path. Reaching this branch requires
# leaving dry-run, since every dry-run branch exits before the log tempfile; a mktemp
# that refuses only the log leaves the goal tempfile and the write intact.
#
# Nothing is spawned, because the refusal comes before the nohup. The assertion
# requires a recorded body and its path in stderr, so a fixture that refused every
# mktemp would fail here rather than pass.
logfail_dir = tempfile.mkdtemp(prefix="finalize-logfail.")
done = run(dry_run="", log_mktemp_fails=True, handoff_dir=logfail_dir)
recorded = sorted(os.listdir(logfail_dir))
check("a tempfile it cannot make after the handoff is written says where the handoff is",
      done.returncode == REFUSED_RC and len(recorded) == 1
      and os.path.join(logfail_dir, recorded[0]) in done.stderr,
      f"rc={done.returncode} err={done.stderr!r} recorded={recorded!r}")
# The session's own goal is cleared only after every step that can refuse.
check("and a close-out refused before its worker types nothing into its pane",
      "send-keys" not in done.tmux_log, f"tmux_log={done.tmux_log!r}")
shutil.rmtree(logfail_dir, ignore_errors=True)

# On the tmux transport the close-out ends its own session's goal, by typing the immediate `/goal clear`
# into its own pane, every time: whether a goal holds the session is the session's state, not the flag.
# It is typed as every line is typed into the box: emptied, pasted, then Enter on its own.
CLEARS = ["send-keys -t %99 C-u", "load-buffer -b finalize-type-N -", "buffer: /goal clear",
          "paste-buffer -d -b finalize-type-N -t %99 -p", "send-keys -t %99 Enter"]
for flags, name in (((), "without --goal"), (("--goal", "the tests pass"), "with --goal")):
    done = run(dry_run="", launcher_flags=flags)
    typed = re.sub(r"finalize-type-\d+", "finalize-type-N", done.tmux_log).splitlines()
    check(f"a tmux close-out {name} clears its own goal before its worker acts",
          done.returncode == 0 and typed == CLEARS,
          f"rc={done.returncode} err={done.stderr!r} tmux_log={done.tmux_log!r}")

# The worker types into the same pane, so a pane the clear cannot reach refuses the handoff.
done = run(dry_run="", tmux_fail="send-keys")
check("a clear that cannot be typed refuses the handoff and says where it is",
      done.returncode == REFUSED_RC and "cannot type /goal clear into pane %99" in done.stderr
      and "verbatim on disk" not in done.stdout and "/handoffs/" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

done = run()
check("a dry run says it would clear the goal and types nothing",
      "[dry-run] would type /goal clear into %99" in done.stdout and done.tmux_log == "",
      f"out={done.stdout!r} tmux_log={done.tmux_log!r}")

# The iTerm2 transport clears it the same way, in the session the relaunch is written to: the box
# emptied, the text bracketed as a paste, and Enter, each written on its own, so the box does not read
# them as one paste. A uuid no session has is an AppleScript error, so a write that finds none fails.
ITERM_UUID = "5C2E7B1A-0D4F-4E8B-9A3C-6F1D2E7B8A90"
KEYSTROKES = ["write s text (character id 21) newline false",
              '[200~" & "/goal clear" & (character id 27) & "[201~") newline false',
              "write s text (character id 13) newline false"]
done = run(panes=None, iterm=True, dry_run="")
script = done.osascript_log
at = [script.find(line) for line in KEYSTROKES]
check("an iTerm2 close-out types /goal clear into its own session, keystroke by keystroke",
      done.returncode == 0 and f'(unique id of s) is "{ITERM_UUID}"' in script and -1 < at[0] < at[1] < at[2]
      and f'error "no iTerm2 session has unique id " & "{ITERM_UUID}"' in script,
      f"rc={done.returncode} err={done.stderr!r} script={script!r}")
done = run(panes=None, iterm=True, dry_run="", osascript_status=1)
check("a clear that cannot be written to the iTerm2 session refuses the handoff and says where it is",
      done.returncode == REFUSED_RC and f"cannot write /goal clear to iTerm2 session {ITERM_UUID}" in done.stderr
      and "/handoffs/" in done.stderr, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
done = run(panes=None, iterm=True)
check("an iTerm2 dry run says it would clear the goal and writes nothing",
      f"[dry-run] would write /goal clear to iTerm2 session {ITERM_UUID}" in done.stdout
      and done.osascript_log == "", f"out={done.stdout!r} script={done.osascript_log!r}")

# A give-up path with no explicit guard: the detached transport reads its working
# directory with a bare command substitution, so an unlinked cwd exits on that
# command's status instead of the launcher's own refusal.
#
# The case asserts the contract: exit 2 and the path of the preserved handoff. "NOT
# delivered" is the discriminating string, since only the launcher's refusal prints it.
cwdgone_dir = tempfile.mkdtemp(prefix="finalize-cwdgone.")
done = run(panes=None, handoff_dir=cwdgone_dir, cwd_gone=True)
recorded = sorted(os.listdir(cwdgone_dir))
check("a give-up with no guard of its own still exits 2 and names the preserved handoff",
      done.returncode == REFUSED_RC and len(recorded) == 1
      and "NOT delivered" in done.stderr
      and os.path.join(cwdgone_dir, recorded[0]) in done.stderr,
      f"rc={done.returncode} err={done.stderr!r} recorded={recorded!r}")
shutil.rmtree(cwdgone_dir, ignore_errors=True)

# Every transport question is answered from one read of the process table, so a
# failing `ps` refuses the handoff before any transport is chosen. Nothing can then
# be shown to be this session's: not a pane, and not a claude to relaunch with its
# --model or permission flags. The assertion checks the refusal, not only a non-zero
# status.
nocmd_dir = tempfile.mkdtemp(prefix="finalize-nocmd.")
done = run(panes=None, handoff_dir=nocmd_dir, ps_fails=True)
recorded = sorted(os.listdir(nocmd_dir))
check("a process table that cannot be read is refused, not walked as if it were empty",
      done.returncode == REFUSED_RC and len(recorded) == 1
      and "cannot read the process table" in done.stderr
      and os.path.join(nocmd_dir, recorded[0]) in done.stderr,
      f"rc={done.returncode} err={done.stderr!r} recorded={recorded!r}")
shutil.rmtree(nocmd_dir, ignore_errors=True)

# Claude Code sets $CLAUDE_PID for every tool call, and it is the only thing that names
# this session's claude. Without it no claude the walk meets can be shown to be this
# session's, so nothing is handed off, by any transport.
for panes in ("%99 ROOTPID 0", None):
    done = run(panes=panes, export_claude_pid=False)
    check(f"an unset $CLAUDE_PID is refused before the walk ({'pane' if panes else 'no pane'})",
          done.returncode == REFUSED_RC and "$CLAUDE_PID is unset" in done.stderr
          and "transport=" not in done.stdout,
          f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The failure that reported success. Resolving the launcher's own path used to
# concatenate two command substitutions in one assignment, and such an assignment
# carries only the LAST one's status: with `basename` closing the line, a failed
# `cd` into a directory that had been unlinked left status 0 and a malformed
# "/finalize-session". Nothing read it synchronously - the worker starts under
# `nohup ... &` - so the launcher announced a scheduled handoff and exited 0 with
# no worker behind it. An ERR trap cannot catch this; nothing ever failed. The
# assertion is therefore the refusal, and rc 0 is the regression it guards.
selfgone_dir = tempfile.mkdtemp(prefix="finalize-selfgone.")
done = run(handoff_dir=selfgone_dir, self_dir_gone=True)
recorded = sorted(os.listdir(selfgone_dir))
check("a launcher whose own directory is gone refuses instead of scheduling nothing",
      done.returncode == REFUSED_RC and "cannot resolve own directory" in done.stderr
      and "handoff scheduled" not in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r} recorded={recorded!r}")
shutil.rmtree(selfgone_dir, ignore_errors=True)

# Both writes are guarded, so a failed redirection exits 2 with the launcher's
# message rather than bash's status and message.
#
# The handoff write, reached through an existing directory that refuses writes.
# Nothing is recorded, so the refusal must not name a preserved handoff. The write
# comes before the close-out is journaled, so the journal holds no close-out for a
# handoff that never existed, and the session's summary is still there for the next run.
rodir = tempfile.mkdtemp(prefix="finalize-ro.")
handoffs_ro = os.path.join(rodir, "handoffs")
os.mkdir(handoffs_ro, 0o500)
journal_ro = os.path.join(rodir, "journal.jsonl")
summarize(journal_ro, "handed off")
try:
    done = subprocess.run(
        [LAUNCHER, "a real handoff"], text=True, capture_output=True, timeout=30,
        env={"PATH": REAL_DIRS, "HOME": rodir, "TMPDIR": rodir,
             "MEMENTO_HANDOFF_DIR": handoffs_ro, "MEMENTO_JOURNAL": journal_ro,
             "CLAUDE_CODE_SESSION_ID": SESSION_ID})
    # Assert the launcher's full sentence: bash's redirection error also contains the
    # launcher's name, so a bare prefix match would not separate the two.
    check("a handoff it cannot write is refused as the launcher, not as the shell",
          done.returncode == REFUSED_RC
          and "finalize-session: cannot write the handoff to" in done.stderr
          and "preserved at" not in done.stderr,
          f"rc={done.returncode} err={done.stderr!r}")
    check("and it journals no close-out, so the summary is still the next run's to take",
          os.listdir(handoffs_ro) == []
          and [type(entry) for entry in entries(journal_ro)] == [Summary],
          f"handoffs={os.listdir(handoffs_ro)!r} journal={entries(journal_ro)!r}")
finally:
    os.chmod(handoffs_ro, 0o700)
    shutil.rmtree(rodir, ignore_errors=True)

# The goal write comes after the handoff write, so its refusal must name the preserved
# handoff. The path is read back from the handoff body, and the refusal is checked
# against that file.
done, bodies = argv_case("a real handoff", path_prefix=BAD_GOALPATH_BIN)
msgpath = bodies[0].rsplit("This handoff verbatim on disk: ", 1)[-1].strip() if bodies else ""
check("a carried goal it cannot write still says where the handoff is",
      done.returncode == REFUSED_RC and len(bodies) == 1 and msgpath in done.stderr
      and "NOT delivered" in done.stderr,
      f"rc={done.returncode} err={done.stderr!r} msgpath={msgpath!r}")

# --model and --effort are asked for, never defaulted: the transport's own behavior stands
# unless the caller names a model or effort.
done, _ = argv_case("a handoff")
check("without --model or --effort nothing is requested",
      "requested model: (none) effort: (none)" in done.stdout, done.stdout)
done, _ = argv_case("--model", "opus[1m]", "--effort", "high", "a handoff")
check("--model and --effort are carried into the reset session",
      "requested model: opus[1m] effort: high" in done.stdout, done.stdout)
done, bodies = argv_case("--effort", "max", "a handoff")
check("one of the two can be asked for without the other, and neither is handoff text",
      "requested model: (none) effort: max" in done.stdout
      and len(bodies) == 1 and "--effort" not in bodies[0],
      f"out={done.stdout!r} handoffs={bodies!r}")
for bad in (["--model"], ["--model", "--goal", "x"], ["--effort", ""], ["--model", "two words"],
            ["--effort", "-high"]):
    done, bodies = argv_case(*bad, "a handoff")
    check(f"a value that is not one word is refused and records nothing: {bad!r}",
          done.returncode == REFUSED_RC and "single-word value" in done.stderr and bodies == [],
          f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

done, bodies = argv_case("ordinary handoff text")
check("an ordinary message is still recorded",
      done.returncode == 0 and len(bodies) == 1 and "ordinary handoff text" in bodies[0],
      f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

# The handoff names the session that wrote it, so the next agent can read that
# transcript back, on the line above the path, which stays last. Without an id the
# handoff still goes, and that line names the variable it was looked for in. The
# checks hold the line's place and content, not its wording.
for session_id, expected, what in (
        (SESSION_ID, SESSION_ID, "names the outgoing session's id"),
        (None, "CLAUDE_CODE_SESSION_ID", "with no session id still hands off, naming the variable"),
        ("", "CLAUDE_CODE_SESSION_ID", "with an empty session id still hands off, naming the variable")):
    done, bodies = argv_case("a handoff", session_id=session_id)
    tail = bodies[0].splitlines()[-2:] if len(bodies) == 1 else []
    check(f"the handoff {what} on the line above the path",
          done.returncode == 0 and len(tail) == 2 and expected in tail[0]
          and tail[1].startswith("This handoff verbatim on disk: "),
          f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

# --- resolution through a real ancestry -----------------------------------

for depth in (0, 1, 5):
    done = run(depth=depth)
    check(f"resolves the pane {depth + 1} process(es) up",
          picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The walk examines this process and 15 ancestors; `nest DEPTH` puts the
# published pid at distance DEPTH+1. These two pin that bound from both sides.
done = run(depth=14)
check("resolves a pane at the last hop inside the 16-hop bound",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

done = run(depth=15)
check("refuses a pane one hop beyond the 16-hop bound", picked(done) == DECLINED, picked(done))

done = run(depth=3, panes="%1 4 0\n%99 ROOTPID 0\n%7 5 0")
check("picks the pane that owns it, not the first pane listed",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r}")

# A daemon respawns a session it hosts whose process exits on a signal, beside its
# successor, so a hosted session is retired through `claude stop` of the id the daemon's
# listing gives it, and a session no claude hosts is signalled without reading the listing.
done = run(panes=None, agents_status=1)
check("a session no claude hosts is retired by signal, and never needs the session listing",
      picked(done) == DETACHED and "[dry-run] retirement: signal\n" in done.stdout,
      f"out={done.stdout!r} err={done.stderr!r}")
done = run(depth=2, rehost_at=2, claude_at=1, panes=None)
check("a hosted session is retired through `claude stop` of the id the session listing gives it",
      re.search(rf"^\[dry-run\] retirement: stop \S+/claude {BACKGROUND_ID}$", done.stdout, re.M),
      f"out={done.stdout!r} err={done.stderr!r}")
for name, case, reason in (
        ("does not name it", dict(agents="[]"), "holds 0 background sessions"),
        ("cannot be read", dict(agents="Error: daemon not running"), "not JSON"),
        ("fails", dict(agents_status=1), "agents --json' exited 1")):
    done = run(depth=2, rehost_at=2, claude_at=1, panes=None, **case)
    check(f"a hosted session whose session listing {name} is refused, since a signal would have it respawned",
          done.returncode == REFUSED_RC and reason in done.stderr and "a daemon respawns one that is signalled" in done.stderr
          and "[dry-run] transport=" not in done.stdout, f"out={done.stdout!r} err={done.stderr!r}")

# A daemon host is a pane boundary: a hosted session must not deliver into a pane
# above its host, which shows the spawner's session. The chain is pane shell,
# spawner claude, `claude --bg-pty-host`, session claude, shell. Both claudes are
# planted, so the finder has the spawner's to wrongly pick.
PANES_ABOVE_HOST = f"%99 ROOTPID 0\n%77 {STRANGER.pid} 0"
done = run(depth=5, rehost_at=3, claude_at=[5, 2], panes=PANES_ABOVE_HOST)
check("a hosted session never discovers the pane above its host; it goes detached",
      picked(done) == DETACHED, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
check("and it relaunches its own claude below the host, not the spawner's",
      done.session_claude_pid is not None and done.session_claude_pid != done.root_pid
      and f"claude_pid={done.session_claude_pid} " in done.stdout,
      f"session={done.session_claude_pid} root={done.root_pid} out={done.stdout!r}")
# The daemon passed its spawner's live $TMUX_PANE down. The inherited spelling
# must lose to the boundary exactly as discovery does.
done = run(depth=5, rehost_at=3, claude_at=[5, 2], tmux_pane="%77", host_keeps_tmux=True,
           panes=PANES_ABOVE_HOST)
check("an inherited $TMUX_PANE from the daemon's spawner is never used",
      picked(done) == DETACHED, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# Something between the session's claude and its host (a wrapper) does not
# hide the host: any host crossed below the first pane counts.
done = run(depth=5, rehost_at=4, claude_at=[5, 2], panes=PANES_ABOVE_HOST)
check("a wrapper between the session and its host does not hide the host",
      picked(done) == DETACHED, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# $CLAUDE_PID names the spawner's claude above the host. Relaunching that would kill
# an unrelated session, so the walk refuses at the host it meets first.
done = run(depth=2, rehost_at=1, panes=PANES_ABOVE_HOST)
check("the walk never climbs past a host to a $CLAUDE_PID above it",
      picked(done) == DECLINED and "it met claude process" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The control: the same chain with no host discovers %99, so the refusals above
# come from the boundary, not from depth or the fixture.
done = run(depth=5, panes=PANES_ABOVE_HOST)
check("the same chain without a host discovers the pane above it",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A host above the pane hosts some other session: this one keeps its pane. This
# is also the suite run from inside a background session.
done = run(depth=5, panes=PANES_ABOVE_HOST, outer=CLAUDE_BIN)
check("a host above this session's pane does not make it hosted",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A handoff message naming a host flag is prompt text, not a host.
done = run(depth=5, panes=PANES_ABOVE_HOST, message="next: verify the --bg-pty-host boundary")
check("a handoff message containing --bg-pty-host does not trip the boundary",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The boundary is read from the shape of the tree: a claude other than this session's
# own, met before a live pane, means the pane shows that claude's session.
# A claude started inside another claude's shell, below the outer one's pane.
done = run(depth=5, claude_at=[4, 2], panes=PANES_ABOVE_HOST)
check("a claude inside another claude never takes the outer one's pane",
      picked(done) == DETACHED, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The same, where the pane's own process is the outer claude.
done = run(depth=5, claude_at=[5, 2], panes=PANES_ABOVE_HOST)
check("a pane whose process is another claude is never this session's",
      picked(done) == DETACHED, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A claude is known by the executable it runs as, under each name an install gives it.
# Above $CLAUDE_PID one confines the session, and below it the walk refuses, since
# $CLAUDE_PID was not reached first. An npm install runs as node, so any node above the
# session confines it too: the safe direction, accepted.
for name, bin_ in (("claude", CLAUDE_BIN), ("2.1.299", VERSIONED_CLAUDE_BIN),
                   (".claude-wrapped", WRAPPED_CLAUDE_BIN), ("node", NODE_BIN)):
    done = run(depth=5, rehost_at=3, claude_at=[2], panes=PANES_ABOVE_HOST, host_bin=bin_)
    check(f"a process running as {name} above $CLAUDE_PID confines the session",
          picked(done) == DETACHED and "hosted by another claude" in ancestry(done),
          f"ancestry={ancestry(done)!r} out={done.stdout!r} err={done.stderr!r}")
    done = run(depth=2, claude_at=[2], rehost_at=1, panes=None, host_bin=bin_)
    check(f"a process running as {name} below $CLAUDE_PID refuses the handoff",
          done.returncode == NO_TRANSPORT_RC and "it met claude process" in done.stderr,
          f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
    # $CLAUDE_PID is passed by pid whatever it runs as, so its own name never counts against it.
    done = run(depth=2, claude_at=[1], claude_bin=bin_)
    check(f"this session's own claude running as {name} keeps its pane",
          picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# Names near a claude's are not one: a version needs three parts, and a shell is a shell.
for name, bin_ in (("2.1", NOT_CLAUDE_BIN), ("zsh", ZSH_BIN)):
    done = run(depth=5, rehost_at=3, claude_at=[2], panes=PANES_ABOVE_HOST, host_bin=bin_)
    check(f"a process running as {name} above this session is walked past to its pane",
          picked(done) == "%99", f"ancestry={ancestry(done)!r} out={done.stdout!r} err={done.stderr!r}")

# A $CLAUDE_PID beyond the 16-hop bound is unproven, so relaunching it could signal a
# session that never asked: it is refused, not relaunched.
done = run(depth=17, claude_at=[16], panes=None)
check("a $CLAUDE_PID beyond the walk's reach is refused, not relaunched",
      done.returncode == NO_TRANSPORT_RC and "the ancestry walk never passed $CLAUDE_PID" in done.stderr
      and "16-hop bound" in done.stderr and f"claude_pid={done.session_claude_pid} " not in done.stdout,
      f"ancestry={ancestry(done)!r} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A pane reached before $CLAUDE_PID shows no session of this one's, so nothing is typed into it.
done = run(depth=2, claude_at=[], claude_pid=STRANGER.pid)
check("a pane reached before $CLAUDE_PID is refused, not cleared",
      done.returncode == NO_TRANSPORT_RC and "the ancestry walk never passed $CLAUDE_PID" in done.stderr
      and picked(done) != "%99",
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A tmux server the walk climbs through lists no live pane of this session (another
# socket, or a tmux that cannot report it), so whatever terminal shows this session is
# not its own to type into: confined, never paneless. ps reads the executable of every
# owner's process, so a root-owned server is known too.
for name, bin_ in (("tmux", TMUX_SERVER_BIN), (".tmux-wrapped", WRAPPED_TMUX_BIN)):
    done = run(depth=2, claude_at=[1], panes=None, rehost_at=2, host_bin=bin_)
    check(f"a tmux server running as {name} above this session with no pane of it confines the session",
          ancestry(done).startswith("confined, tmux process") and picked(done) == DETACHED,
          f"ancestry={ancestry(done)!r} out={done.stdout!r} err={done.stderr!r}")
# $CLAUDE_PID is matched as the pid's exact string: awk would compare `0<pid>` with
# <pid> as numbers. The runner is on every chain's ancestry and is given a pane here,
# so a numeric match would take it.
done = run(panes=f"%55 {os.getpid()} 0", claude_pid=f"0{os.getpid()}", claude_at=[])
check("a $CLAUDE_PID that only numerically equals an ancestor's pid is never passed",
      done.returncode == NO_TRANSPORT_RC and "the ancestry walk never passed $CLAUDE_PID 0" in done.stderr
      and picked(done) != "%55",
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The hop bound itself: a walk that ends there has not shown that nothing above this
# session shows another one, so it is confined, and no inherited terminal is trusted.
# The own claude sits low in the chain, so the walk has passed it before the bound.
done = run(depth=15, panes=None, claude_at=[1])
check("a walk that ends at its hop bound confines the session",
      ancestry(done).startswith("confined, the walk ended at its 16-hop bound")
      and picked(done) == DETACHED,
      f"ancestry={ancestry(done)!r} out={done.stdout!r} err={done.stderr!r}")

# --- a session `claude remote-control` spawned ----------------------------------
# The server is the host: the session's claude is a `--print` child directly below it, whose input is
# the server's pipe, and whose argv holds the session's URL. Claude Code also marks its environment
# with the bridge kind, which is inherited and so decides nothing. The chain is pane shell, the server,
# the session's claude, shell. Each argv is the vector its process runs with, so a prompt is one
# argument however it reads.


def remote_picked(done):
    """The session URL the launcher would post to, or None when it picked another transport."""
    for line in done.stdout.splitlines():
        if line.startswith("[dry-run] transport=remote-control session="):
            return line.split("session=", 1)[1].split(" ", 1)[0]
    return None


HOSTED = dict(depth=3, rehost_at=3, claude_at=[2], panes=PANES_ABOVE_HOST)
for name, bin_, host, session in (("", CLAUDE_BIN, SERVER_ARGV, "cse_resets01"),
                                  (" by a node install", NODE_BIN, ["cli.js"] + SERVER_ARGV, "cse_resets06")):
    done = run(**HOSTED, entrypoint="sdk-cli", bridge=session, host_bin=bin_, host_argv=host)
    check(f"a session a remote-control server{name} spawned posts to its own session, not a pane or a relaunch",
          remote_picked(done) == session_url(session) and ancestry(done).startswith("hosted, "),
          f"ancestry={ancestry(done)!r} out={done.stdout!r} err={done.stderr!r}")
    check(f"and reads that session's log head before it returns, in a dry run too{name}",
          f"log_head={fake_session_server.LOGS[session].head() - 2} " in done.stdout
          and [r[:2] for r in fake_session_server.REQUESTS[session]]
          == [("GET", f"/v1/code/sessions/{session}/events?sort_order=desc&limit=1")], done.stdout)
# The controls: a host whose first argument is no `remote-control`. A daemon started from a
# remote-control session's shell passes the bridge kind down, and a session's claude may hold a
# spawned claude's arguments, so neither makes it a remote-control session. Nor does a claude whose
# prompt holds the word, as one a tool call started with that prompt does.
for name, host in (("a daemon host's session", DAEMON_ARGV),
                   ("a session hosted by a claude whose prompt holds remote-control",
                    ["fix the remote-control transport"])):
    done = run(**HOSTED, bridge="cse_quoted01", host_argv=host)
    check(f"{name} is relaunched in a detached window, though it holds a session's --sdk-url",
          picked(done) == DETACHED and remote_picked(done) is None and ancestry(done).startswith("hosted, "),
          f"ancestry={ancestry(done)!r} out={done.stdout!r} err={done.stderr!r}")
# Nothing but a host's argv and its own is read, so a pane's claude or one further below another
# claude is never posted to, whatever it holds.
done = run(bridge="cse_quoted02")
check("a pane's session that holds a session's --sdk-url is cleared in its pane, never posted to",
      picked(done) == "%99" and remote_picked(done) is None, f"out={done.stdout!r} err={done.stderr!r}")
done = run(depth=4, rehost_at=4, claude_at=[2], panes=PANES_ABOVE_HOST, bridge="cse_resets02")
check("a claude started inside a remote-control session never posts to that session",
      remote_picked(done) is None and picked(done) == DETACHED and ancestry(done).startswith("confined, ")
      and "another claude (process " in ancestry(done) and "is further up" in ancestry(done),
      f"ancestry={ancestry(done)!r} out={done.stdout!r} err={done.stderr!r}")
# A remote-control server's child that names no session is reached by no transport, and a relaunch
# would take it from the server. A prompt that quotes the flag is one argument, not the flag.
for name, own in (("names no session", ["--print"]),
                  ("only quotes one in a prompt", ["--print", f"see --sdk-url {session_url('cse_resets05')}"])):
    done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_resets05", claude_argv=own)
    check(f"a remote-control server's session whose argv {name} refuses",
          done.returncode == REFUSED_RC and "cannot tell which Remote Control session" in done.stderr
          and "gives []" in done.stderr, f"rc={done.returncode} err={done.stderr!r}")
done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_resets03", launcher_flags=("--goal", "the tests pass"))
check("the dry run carries the goal it would post",
      "[dry-run] carried goal: the tests pass" in done.stdout, f"out={done.stdout!r}")

# The real path, against the fake session server. The session reads a prompt posted mid-turn as text,
# handed to the running turn at its next tool call, so every slash command is posted only once the log
# shows the session idle.


def requests(session):
    """What the session server received, as (method, path, Authorization)."""
    return [received[:3] for received in fake_session_server.REQUESTS.get(session, [])]


def posted(session):
    log = fake_session_server.LOGS.get(session)
    return log.prompts() if log else []


handoffs = tempfile.mkdtemp(prefix="finalize-remote.")
try:
    done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_resets04", dry_run="", handoff_dir=handoffs,
               message="carry this forward", launcher_flags=("--goal", "the tests pass", "--effort", "high"))
    seen = requests("cse_resets04")
    # The remote worker's per-session lock sits beside the handoff, so only the handoff is read.
    written = [open(os.path.join(handoffs, name)).read() for name in os.listdir(handoffs) if name.endswith(".md")]
    check("the launcher reads the log's head and schedules the handoff for the turn's end",
          done.returncode == 0
          and f"handoff scheduled → remote-control session {session_url('cse_resets04')}" in done.stdout
          and seen and "sort_order=desc" in seen[0][1],
          f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r} seen={seen!r}")
    check("then posts /clear, the setting, the handoff from its file, and the goal, in that order",
          posted("cse_resets04") == ["/clear", "/effort high", written[0] if written else None,
                                     "/goal the tests pass"], f"seen={seen!r} log={done.worker_log!r}")
    check("each to a session the log shows idle",
          all(idle for _, idle, _ in fake_session_server.LOGS["cse_resets04"].posts),
          fake_session_server.LOGS["cse_resets04"].posts)
    check("after an interrupt aimed at the asking turn, which had ended, so it stopped nothing",
          fake_session_server.LOGS["cse_resets04"].interrupts == [(("asking-turn",), None)],
          fake_session_server.LOGS["cse_resets04"].interrupts)
    check("every request carries the stored login",
          {auth for _, _, auth in seen} == {f"Bearer {fake_session_server.TOKEN}"}, seen)
finally:
    shutil.rmtree(handoffs, ignore_errors=True)

done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_flaky01", dry_run="")
check("a read of the log that gets no answer is retried, and the handoff still lands",
      posted("cse_flaky01")[:1] == ["/clear"] and len(posted("cse_flaky01")) == 2
      and "HTTP 502" in done.worker_log and "retrying" in done.worker_log,
      f"seen={requests('cse_flaky01')!r} log={done.worker_log!r}")

done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_busy01", dry_run="")
check("an asking turn the log never ends gets no /clear, which it would read as text",
      done.returncode == 0 and posted("cse_busy01") == [], f"seen={requests('cse_busy01')!r} out={done.stdout!r}")
check("and the worker's log says the /clear was not posted and where the handoff is kept",
      "the /clear was NOT posted" in done.worker_log and ".md" in done.worker_log, done.worker_log)

done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_stalls01", dry_run="")
check("a reset the log never records holds the handoff back, so it never lands in the old conversation",
      done.returncode == 0 and posted("cse_stalls01") == ["/clear"], f"seen={requests('cse_stalls01')!r}")
check("and the worker's log says the handoff was not posted and where it is kept",
      "the handoff was NOT posted" in done.worker_log and ".md" in done.worker_log, done.worker_log)

done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_refuses01", dry_run="")
check("a session whose log refuses the login refuses the handoff in front of the agent, naming the answer",
      done.returncode == REFUSED_RC and "HTTP 403" in done.stderr and "fixture refuses" in done.stderr
      and "NOT delivered" in done.stderr, f"rc={done.returncode} err={done.stderr!r}")
check("and nothing is posted to it", posted("cse_refuses01") == [], requests("cse_refuses01"))

done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_held01", dry_run="")
check("an asking turn a goal holds is ended by an interrupt aimed at it, and the handoff lands",
      done.returncode == 0 and fake_session_server.LOGS["cse_held01"].interrupts == [(("asking-turn",), ("asking-turn",))]
      and posted("cse_held01")[:1] == ["/clear"] and len(posted("cse_held01")) == 2,
      f"log={done.worker_log!r} interrupts={fake_session_server.LOGS['cse_held01'].interrupts!r}")

# A goal an earlier handoff's worker published for the session, waiting to post it once the handoff's turn
# ends, belongs to the successor once that turn closes out. The publication is held by this suite, so its
# claim sees a live publisher.
claims = tempfile.mkdtemp(prefix="finalize-claims.")
try:
    publication = remote_control.publish(claims, "cse_claims01", "the old goal")
    done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_claims01", dry_run="", handoff_dir=claims)
    check("a close-out on a session with a published goal claims it and carries it on",
          done.returncode == 0 and posted("cse_claims01")[-1:] == ["/goal the old goal"]
          and not publication.take_back(), f"rc={done.returncode} err={done.stderr!r} log={done.worker_log!r}")
    publication = remote_control.publish(claims, "cse_claims02", "the old goal")
    done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_claims02", dry_run="", handoff_dir=claims,
               launcher_flags=("--goal", "the new goal"))
    check("one with a goal of its own claims the published one and drops it, saying so",
          done.returncode == 0 and "dropped for this close-out's own: /goal the old goal" in done.stdout
          and posted("cse_claims02")[-1:] == ["/goal the new goal"] and not publication.take_back(),
          f"out={done.stdout!r} log={done.worker_log!r}")
    publication = remote_control.publish(claims, "cse_refuses02", "the old goal")
    done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_refuses02", dry_run="", handoff_dir=claims)
    check("one refused before the claim leaves the published goal with its worker",
          done.returncode == REFUSED_RC and publication.take_back(), f"rc={done.returncode} err={done.stderr!r}")
    publication = remote_control.publish(claims, "cse_claims03", "the old goal")
    done = run(**HOSTED, entrypoint="sdk-cli", bridge="cse_claims03", dry_run="", handoff_dir=claims,
               goal_rewrite_fails=True)
    check("one whose step after the claim fails refuses, naming the claimed goal to set by hand",
          done.returncode == REFUSED_RC and "/goal the old goal" in done.stderr and not publication.take_back()
          and posted("cse_claims03") == [], f"rc={done.returncode} err={done.stderr!r}")
finally:
    shutil.rmtree(claims, ignore_errors=True)

# The worker's first post interrupts the asking turn, which would abort the launcher's Bash call were it
# still running, so the worker posts nothing while the launcher lives.
launcher = subprocess.Popen(["sleep", "30"])
identity = subprocess.run(["ps", "-o", "lstart=,ucomm=", "-p", str(launcher.pid)],
                          text=True, capture_output=True).stdout.rstrip("\n")
waits = tempfile.mkdtemp(prefix="finalize-waits.")
login_dir = os.path.join(waits, "config")
os.mkdir(login_dir)
with open(os.path.join(login_dir, ".credentials.json"), "w") as handle:
    handle.write(LOGIN)
for name, text in (("handoff.md", "the handoff"), ("goal", "")):
    with open(os.path.join(waits, name), "w") as handle:
        handle.write(text)
waiting = fake_session_server.log_of("cse_waits01")
worker = subprocess.Popen([LAUNCHER, "--remote-worker", session_url("cse_waits01"), str(waiting.head()),
                           os.path.join(waits, "handoff.md"), os.path.join(waits, "goal"), str(launcher.pid), identity],
                          env={**os.environ, "PATH": KEYCHAIN_DIR + ":" + REAL_DIRS, "CLAUDE_CONFIG_DIR": login_dir,
                               "MEMENTO_IDLE_TIMEOUT_SECONDS": "5", "MEMENTO_RESET_TIMEOUT_SECONDS": "5"},
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
check("the remote worker posts nothing while the launcher that started it lives",
      worker.poll() is None and not fake_session_server.REQUESTS.get("cse_waits01"),
      fake_session_server.REQUESTS.get("cse_waits01"))
launcher.kill()
launcher.wait()
worker.wait(30)
check("and interrupts the asking turn once it has exited, then delivers",
      waiting.interrupts == [(("asking-turn",), ("asking-turn",))] and waiting.prompts() == ["/clear", "the handoff"],
      (waiting.interrupts, waiting.prompts()))
shutil.rmtree(waits, ignore_errors=True)


# --- no pane to be had, but tmux still gives a transport --------------------
# Refusing a pane is still correct, but it does not mean refusing to deliver: the
# launcher falls back to the detached transport whenever a claude process can be
# found. Every chain here plants one at its top hop, so DECLINED remains only where
# tmux cannot be reached to spawn a fresh window.

done = run(panes=None)
check("no tmux server: falls to the detached transport", picked(done) == DETACHED, picked(done))
# Which claude, exactly. DETACHED alone cannot distinguish the planted process from
# an ambient claude above the test runner, so the pid is named.
check("the detached transport retires the planted claude, not an ambient one",
      f"claude_pid={done.root_pid} " in done.stdout,
      f"root_pid={done.root_pid} out={done.stdout!r} err={done.stderr!r}")
# The launcher captures what the pid is holding and hands it to the worker to re-check
# before any kill. The captured text must describe the process actually found; a
# constant or the bare pid would pass a non-empty check and prove nothing.
check("the launcher captures the old process's identity, not just its pid",
      any(line.startswith("[dry-run] old-process identity: ") and line.rstrip().endswith(" claude")
          for line in done.stdout.splitlines()),
      f"out={done.stdout!r}")

# $CLAUDE_PID names the session's own pid for every tool call, and it is this session's
# only if the walk passes it. This named claude is a live claude outside the launcher's
# ancestry. It runs in its own session so the whole group can be terminated, since
# nest runs its command as a child.
NAMED_CLAUDE = subprocess.Popen(
    [NEST_BIN, "0", "sleep", "600"],
    env={**os.environ, "NEST_CLAUDE_AT": "0", "NEST_CLAUDE_BIN": CLAUDE_BIN},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
# The exec happens during nest's startup, after Popen returns. Without waiting for it,
# the launcher would read the process as bash and refuse for a timing reason.
for _ in range(100):
    if subprocess.run(["ps", "-o", "ucomm=", "-p", str(NAMED_CLAUDE.pid)],
                      capture_output=True, text=True).stdout.strip() == "claude":
        break
    time.sleep(0.05)
try:
    # With no pane, the walk ends at the chain's planted claude, or with none planted at
    # whatever the runner's ancestry ends in: another claude or init. Either way it has
    # shown the named claude is outside this tree, so it is never relaunched.
    for claude_at, where in ((None, "another claude"), ([], "the end of the ancestry")):
        done = run(panes=None, claude_pid=NAMED_CLAUDE.pid, claude_at=claude_at)
        check(f"a $CLAUDE_PID the walk reaches {where} without passing is refused, not relaunched",
              done.returncode == NO_TRANSPORT_RC and "the ancestry walk never passed $CLAUDE_PID" in done.stderr
              and f"claude_pid={NAMED_CLAUDE.pid} " not in done.stdout,
              f"named={NAMED_CLAUDE.pid} root_pid={done.root_pid} rc={done.returncode} "
              f"out={done.stdout!r} err={done.stderr!r}")
    # A pane is this session's only when its own claude sits at or below it. The named
    # claude is not under the chain's pane, and no other claude is planted, so the walk
    # has shown $CLAUDE_PID contradicts the tree: neither the pane nor the named claude
    # is taken, and the named claude is not relaunched.
    done = run(claude_pid=NAMED_CLAUDE.pid, claude_at=[])
    check("a pane the walk reaches without passing the $CLAUDE_PID claude is refused, not taken or relaunched",
          done.returncode == NO_TRANSPORT_RC and "the ancestry walk never passed $CLAUDE_PID" in done.stderr
          and f"claude_pid={NAMED_CLAUDE.pid} " not in done.stdout and picked(done) != "%99",
          f"named={NAMED_CLAUDE.pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
finally:
    os.killpg(NAMED_CLAUDE.pid, 15)
    NAMED_CLAUDE.wait()

# --- what the relaunch starts in: the session's transcript ------------------------
# The relaunching transports read the permission mode and effort the transcript records
# last, through lib/transcript.py, whose own suite covers every record shape. These
# cases pin the launcher's half: the flags reach the relaunch line, and every way the
# transcript or the environment fails to say what to relaunch with is a refusal.


def flags(done):
    """The relaunch flags the dry run reports, or None when it reports none."""
    for line in done.stdout.splitlines():
        if line.startswith("[dry-run] transport=") and " flags=[" in line:
            return line.split(" flags=[", 1)[1].rsplit("]", 1)[0]
    return None


done = run(panes=None)
check("a detached relaunch starts in the recorded permission mode and effort",
      picked(done) == DETACHED and flags(done) == "--permission-mode bypassPermissions --effort high",
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The session-state batch records a mode switched after the last prompt.
for records, mode in (([prompt("default"), answer("high"), state("acceptEdits")], "acceptEdits"),
                      ([state("acceptEdits"), prompt("default"), answer("high")], "default")):
    done = run(panes=None, transcripts=(records,))
    check(f"the newer of a prompt record and a permission-mode record wins: {mode}",
          flags(done) == f"--permission-mode {mode} --effort high",
          f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A model with no effort level records none, and the relaunch passes none.
done = run(panes=None, transcripts=([prompt("plan"), answer(None)],))
check("a session that records no effort relaunches with no --effort",
      picked(done) == DETACHED and flags(done) == "--permission-mode plan",
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A requested model and effort come after the recorded flags, so the request wins.
done = run(panes=None, launcher_flags=("--model", "opus[1m]", "--effort", "max"))
check("a requested model and effort land after the recorded flags",
      flags(done) == "--permission-mode bypassPermissions --effort high --model opus\\[1m\\] --effort max",
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A launcher reached through a symlink finds the reader beside its real path.
done = run(panes=None, via_link=True)
check("a launcher run through a symlink reads the transcript",
      picked(done) == DETACHED and flags(done) == "--permission-mode bypassPermissions --effort high",
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The pane transport clears the session in place, so it reads no transcript.
done = run(transcripts=(), session_id=None)
check("the pane transport hands off with no transcript and no session id",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The iTerm2 transport relaunches the same way.
done = run(panes=None, iterm=True)
check("an iTerm2 relaunch starts in the recorded permission mode and effort",
      "[dry-run] transport=iTerm2 " in done.stdout
      and flags(done) == "--permission-mode bypassPermissions --effort high"
      and re.search(rf"would relaunch: \(export CLAUDE_CONFIG_DIR=\S+; claude --session-id {UUID} "
                    r"--permission-mode bypassPermissions --effort high ", done.stdout),
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The iTerm2 line is typed into the session's own shell, so a `claude` that shell defines as
# an alias is the claude it runs.
line = done.stdout.partition("would relaunch: ")[2].partition("\n")[0]
aliased = subprocess.run(["bash", "-c", 'shopt -s expand_aliases; alias claude="echo ALIASED"\n' + line],
                         capture_output=True, text=True)
check("an iTerm2 relaunch runs the shell's own claude alias",
      aliased.returncode == 0 and re.match(rf"ALIASED --session-id {UUID} ", aliased.stdout),
      f"line={line!r} ran={aliased!r}")
# A session with no CLAUDE_CONFIG_DIR relaunches with none, whatever the new window carries.
done = run(panes=None, config_unset=True)
check("a session with no config dir relaunches with it unset, not set to ~/.claude",
      re.search(rf"would launch: \(unset CLAUDE_CONFIG_DIR; \S+ --session-id {UUID} ", done.stdout),
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# Every way the session cannot say what to relaunch with is a refusal through _refuse,
# for both relaunching transports, with the handoff preserved and the reason named.
REFUSALS = [
    ("no transcript", dict(transcripts=()), "0 transcripts for session"),
    ("two transcripts", dict(transcripts=(BYPASS_HIGH, BYPASS_HIGH)), "2 transcripts for session"),
    ("no mode recorded", dict(transcripts=([answer("high")],)), "carries permissionMode"),
    ("an unknown mode", dict(transcripts=([prompt("yolo"), answer("high")],)), "records permissionMode 'yolo'"),
    ("an unknown effort", dict(transcripts=([prompt("plan"), answer("ludicrous")],)), "records effort 'ludicrous'"),
    ("no session id", dict(session_id=None), "$CLAUDE_CODE_SESSION_ID is unset"),
    ("a headless entrypoint", dict(entrypoint="sdk-cli"), "is not an interactive session ($CLAUDE_CODE_ENTRYPOINT is 'sdk-cli'"),
    ("no entrypoint", dict(entrypoint=None), "$CLAUDE_CODE_ENTRYPOINT is ''"),
]
for what, setup, reason in REFUSALS:
    for transport, iterm in (("detached", False), ("iTerm2", True)):
        done = run(panes=None, iterm=iterm, **setup)
        check(f"{what} is refused by the {transport} transport, naming why",
              done.returncode == REFUSED_RC and reason in done.stderr and "NOT delivered" in done.stderr
              and "transport=" not in done.stdout,
              f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The relaunch line is parsed by a shell (tmux's default-shell, or the terminal's). An
# unquoted `--model opus[1m]` is a glob there, which zsh aborts on when nothing matches,
# and a quote in the handoff's path unbalances the line. The dry run prints the line the
# worker runs; it is run through zsh and through bash's failglob with claude swapped for
# a printer of the config dir and argv, and must yield the exact argv.
quoted_dir = tempfile.mkdtemp(prefix="o'neil [1m] ")
done = run(panes=None, handoff_dir=quoted_dir, launcher_flags=("--model", "opus[1m]"))
line = done.stdout.partition("would launch: ")[2].partition(" in a fresh detached")[0]
bin_end = line.find(" --session-id")
printing = line[:bin_end].rpartition("; ")[0] + '; printf "%s\\n" "$CLAUDE_CONFIG_DIR"' + line[bin_end:]
shells = [["bash", "-O", "failglob", "-c"]] + ([["zsh", "-f", "-c"]] if shutil.which("zsh") else [])
for shell in shells:
    parsed = subprocess.run(shell + ['cat() { printf "%s\\n" "$@"; }; ' + printing],
                            capture_output=True, text=True)
    argv = parsed.stdout.split("\n")
    check(f"the relaunch line reaches {shell[0]} as the exact argv, glob-shaped model and quoted path included",
          picked(done) == DETACHED and parsed.returncode == 0 and bin_end > 0
          and argv[0] != "" and argv[1] == "--session-id"
          and argv[3:9] == ["--permission-mode", "bypassPermissions", "--effort", "high", "--model", "opus[1m]"]
          and argv[9].startswith(quoted_dir + "/"),
          f"line={line!r} parsed={parsed!r} out={done.stdout!r}")
shutil.rmtree(quoted_dir)

# A named pid that is not a claude is refused, never replaced by the claude the walk
# meets: that planted claude, or a real session in the runner's ancestry, would
# otherwise be terminated in its place.
done = run(panes=None, claude_pid=STRANGER.pid)
check("$CLAUDE_PID naming a non-claude process is refused, not walked past",
      done.returncode == NO_TRANSPORT_RC
      and "the ancestry walk never passed $CLAUDE_PID" in done.stderr
      and f"claude_pid={done.root_pid} " not in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# $CLAUDE_PID holding something that is not a pid at all is a refusal in the
# launcher's own voice, not the shell's `integer expression expected` diagnostic.
done = run(panes=None, claude_pid="abc")
check("a non-numeric $CLAUDE_PID is refused cleanly",
      done.returncode == NO_TRANSPORT_RC and "the ancestry walk never passed $CLAUDE_PID" in done.stderr
      and "integer expression" not in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# An identity read that succeeds with empty output is refused rather than accepted:
# `ps` exits 0 for a pid whose holder exited, and the worker would then compare two
# empty strings and could signal a stranger now holding that pid. The assertion checks
# the refusal reason, since a chain with no claude also exits 2. This must be pinned at
# the launcher, because the worker's `IDENT_NOW=$(_process_identity "$PID") ||
# IDENT_NOW=""` yields the same empty string on failure as on success-with-nothing.
done = run(panes=None, mute_identity=True)
check("an identity read that succeeds but names nobody is a refusal, not an empty identity",
      done.returncode == NO_TRANSPORT_RC
      and "cannot read the identity of claude process" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The control: under the same wrapper, the ancestry hops and the whole-table walk still
# resolve the pane. Without it, a wrapper that broke ps outright would produce the
# refusal above for an unrelated reason.
check("the identity-muting ps leaves the ancestry walk intact",
      picked(run(depth=2, mute_identity=True)) == "%99",
      "without this the mute wrapper, not the identity guard, could be doing the work")

done = run(panes="")
check("a server with no panes falls to the detached transport", picked(done) == DETACHED, picked(done))

done = run(tmux_on_path=False)
check("tmux absent from PATH: an unhosted session declines - nothing left to spawn a window with",
      picked(done) == DECLINED, picked(done))
# The exit code is the no-transport report a caller acts on. It is pinned once here,
# against the unhosted decline, rather than beside every refused-pane case below.
check("declining is reported by exit code, not by prose alone",
      done.returncode == NO_TRANSPORT_RC, f"rc={done.returncode}")
# The decline is the documented recovery route: the handoff is recorded and provably
# undelivered. The notice comes from the shared refusal, so this case pins it too, since
# a refactor could drop the interpolation while the exit code stays intact.
check("and it still says the handoff is preserved, and where",
      "NOT delivered" in done.stderr and ".md" in done.stderr, f"err={done.stderr!r}")
# The decline names each absence the branch is reached on, so the reader knows which of
# the three transports to provide.
check("and it names the three absences: owned pane, iTerm2 session, tmux binary",
      all(absence in done.stderr for absence in
          ("no owned tmux pane", "no reachable iTerm2 session", "no tmux binary")),
      f"err={done.stderr!r}")

# A hosted session with no tmux declines for its own reason: the host, not a missing
# pane, is why no pane or iTerm2 session can show it.
done = run(tmux_on_path=False, outer=CLAUDE_BIN)
check("a hosted session with no tmux binary declines, naming the host as the reason",
      done.returncode == NO_TRANSPORT_RC and "hosted by another claude" in done.stderr
      and "NOT delivered" in done.stderr, f"rc={done.returncode} err={done.stderr!r}")

# A live pane owned by a process that no ancestor accounts for must be refused, not
# claimed. Every other pane in the suite is dead, outside the ancestry, or out of the
# walk's reach, so this is the only case that forces a rejection. Refusing it still
# lands on the detached transport.
done = run(depth=2, panes=f"%5 {STRANGER.pid} 0")
check("a live pane owned by a stranger is refused, not claimed - falls to detached",
      picked(done) == DETACHED, picked(done))

# A pane_pid of 1 is never tested against the ancestry, because the walk stops at init
# (`pid + 0 <= 1`). This pins that termination guard, not the descent match.
done = run(depth=2, panes="%5 1 0")
check("the walk stops at init rather than climbing past it - falls to detached",
      picked(done) == DETACHED, picked(done))

# --- the two forgeries a bare pid match cannot see -------------------------

done = run(depth=2, panes="%99 ROOTPID 1")
check("a dead pane still advertising an ancestor's pid is refused - falls to detached",
      picked(done) == DETACHED, picked(done))

done = run(depth=2, panes="%99 ROOTPID 1\n%98 ROOTPID 0")
check("a live pane is still found past a dead one holding the same pid",
      picked(done) == "%98", f"rc={done.returncode} out={done.stdout!r}")

# The walk keeps one pane per pid, last row winning, so order matters: listed
# live-then-dead, a walk that screens for liveness keeps %98, while one that does not
# lets the dead %99 overwrite it.
done = run(depth=2, panes="%98 ROOTPID 0\n%99 ROOTPID 1")
check("a dead pane listed after a live one does not displace it in the walk",
      picked(done) == "%98", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A pane record with no dead column: `#{pane_dead}` expands to nothing, as in a tmux
# that predates the format. The row then has three fields, and a guard written as
# `$4 != 1` would read the unset $4 as alive and admit the pane. The positive cases
# above supply the column and resolve.
done = run(depth=2, panes="%99 ROOTPID")
check("a pane whose liveness tmux never stated is refused, not assumed - falls to detached",
      picked(done) == DETACHED, picked(done))

# A pid reissued to a process younger than the descendant claiming it ends the walk:
# nothing at or above it is this session's ancestry. These cases forge the chain
# root, which is the pane's process, and plant the own claude one hop below it, so
# refusing the pane still leaves a claude to relaunch.
done = run(depth=2, sleep=2, forge_age="00:00", claude_at=[1])
check("an ancestor younger than its own descendant is refused as a recycled pid - falls to detached",
      picked(done) == DETACHED and "pid was reissued" in ancestry(done), picked(done))
# The control runs under the forging ps, differing only in the value written. Without
# it, a mangled process table could decline cleanly with the no-transport code for a
# reason unrelated to the age check. Only a run that forges and still resolves shows
# the wrapper leaves the subject intact.
check("the same forged pid resolves when the age is older instead of younger",
      picked(run(depth=2, sleep=2, forge_age="99:00:00", claude_at=[1])) == "%99",
      "without this the forging fixture, not the age check, could be doing the work")

# The walk refuses an elapsed time it cannot parse rather than treating it as age zero.
# Pinning that takes a value the format rejects but ageof() still reads as large. `abc`
# folds to zero, which the age check already refuses, so the format guard would go
# untested. `1:2:3:4` has too many fields for [[dd-]hh:]mm:ss yet folds to about 62
# hours, so only the format check can refuse it.
done = run(depth=2, sleep=2, forge_age="1:2:3:4", claude_at=[1])
check("an elapsed time the walk cannot read refuses the pane - falls to detached",
      picked(done) == DETACHED and "cannot be read" in ancestry(done), picked(done))

# The same guard holds for the other two questions the walk answers. A claude at a
# reissued pid is no claude of this session's: it is not the own claude to relaunch,
# since signalling it would hit whatever now holds the pid,
# even with $CLAUDE_PID naming it, and the refusal says why.
done = run(depth=2, sleep=2, forge_age="00:00", panes=None)
check("a claude at a reissued pid is never relaunched as this session's own, though $CLAUDE_PID names it",
      done.returncode == NO_TRANSPORT_RC and "pid was reissued" in done.stderr
      and f"claude_pid={done.root_pid} " not in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# and it is not a host either. The walk's reason names the reissued pid, not a host.
done = run(depth=2, sleep=2, forge_age="00:00", panes=None, claude_at=[1, 2])
check("a claude at a reissued pid above the own claude is read as a reissued pid, not a host",
      picked(done) == DETACHED and "pid was reissued" in ancestry(done)
      and "hosted by" not in ancestry(done),
      f"ancestry={ancestry(done)!r} out={done.stdout!r} err={done.stderr!r}")

# --- the environment's pane ------------------------------------------------
# $TMUX_PANE is inherited, so it can name a pane of some other session: a daemon's
# spawner, a stale id, a dead pane, or the ':.' target an unresolvable id renders
# into. Only the walk's pane is this session's, whatever $TMUX_PANE names. Every value
# here names a pane other than %99, the one the walk reaches; the live ones are a
# stranger's.
ELSEWHERE = f"%42 {STRANGER.pid} 0\n%77 {STRANGER.pid} 1\n%99 ROOTPID 0"
for elsewhere in ("%42", "%4242", "%77", ":."):
    done = run(depth=2, panes=ELSEWHERE, tmux_pane=elsewhere)
    check(f"a $TMUX_PANE naming a pane the walk did not reach ({elsewhere}) loses to the walk's pane",
          picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# With no pane to reach, a live $TMUX_PANE is still not taken.
done = run(depth=2, panes=f"%42 {STRANGER.pid} 0", tmux_pane="%42")
check("a live $TMUX_PANE the walk did not reach is never taken, even with no other pane",
      picked(done) == DETACHED, picked(done))

# --- the detached transport, run for real -------------------------------------
# The launcher cases above run in dry run, so the detached transport's promise is not
# covered there: the successor took the message or the asking session hears it did
# not, and afterwards exactly one agent is live. These cases run the real launcher and
# worker against the fixture tmux, which runs the relaunch line the launcher built,
# and a fixture successor, and each is decided by what the launcher reported and
# whether the old process is alive or dead at the end.

# The ceiling credits a close-out by matching the launcher's success line in a tool
# result. Read from the hook's source, since importing the hook runs it.
CEILING = os.path.join(HERE, "..", "..", "..", "hooks", "scripts", "context-ceiling.py")
RESET_MARKER = next(
    re.compile(node.value.args[0].value) for node in ast.parse(open(CEILING).read()).body
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "RESET_MARKER")


# Long enough for the `late` successor's 3s, short enough that a refusal is quick.
READY_TIMEOUT = 8


def detail(done):
    return (f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r} "
            f"tmux_log={done.tmux_log!r} worker_log={done.worker_log!r}")


# What the delivered run's session says it did, as its agent wrote it to a file: several lines
# that name git and a shell, with blank lines around them.
DID = "- Ran git pull --rebase, then bash -n on the launcher.\n\nIt holds: `sh` isn't $SHELL."
taken_dir = tempfile.mkdtemp(prefix="finalize-taken.")
taken_handoffs = os.path.join(taken_dir, "handoffs")
taken_journal = os.path.join(taken_dir, "journal.jsonl")

# Each case waits out a successor's answer and, when it is refused, the old process's
# linger, so they run concurrently. Each owns its workdir, successor and old process.
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    taken = pool.submit(run, panes=None, successor="records", handoff_dir=taken_handoffs,
                        journal=taken_journal, summary=f"\n{DID}\n\n\n")
    silent = pool.submit(run, panes=None, successor="silent")
    other = pool.submit(run, panes=None, successor="other")
    dies = pool.submit(run, panes=None, successor="dies")
    killed = pool.submit(run, panes=None, successor="killed")
    # A tmux server started from another shell carries its own CLAUDE_CONFIG_DIR, or
    # none; the relaunch line states this session's either way.
    server_differs = pool.submit(run, panes=None, successor="records",
                                 server_config_dir="/nonexistent/server-config")
    unset_here = pool.submit(run, panes=None, successor="records", config_unset=True,
                             server_config_dir="/nonexistent/server-config")
    # Esc on an attached session, or a short Bash timeout, stops the launcher's call
    # mid-wait. The worker, outside that call's tree, still decides.
    stopped_then_taken = pool.submit(run, panes=None, successor="late", kill_launcher_tree=True)
    stopped_never_taken = pool.submit(run, panes=None, successor="silent", kill_launcher_tree=True)
    hosted_taken = pool.submit(run, depth=2, rehost_at=2, claude_at=1, panes=None, successor="records")

done = taken.result()
check("a successor that takes the message is reported delivered, naming its session id",
      done.returncode != REFUSED_RC
      and re.search(rf"handoff delivered → session {UUID} in detached tmux window 'finalize-", done.stdout),
      detail(done))
check("and the line is the one the ceiling credits as a close-out",
      RESET_MARKER.search(done.stdout), f"{RESET_MARKER.pattern!r} vs {done.stdout!r}")
written = [os.path.join(taken_handoffs, name) for name in os.listdir(taken_handoffs)]
kept = entries(taken_journal)
check("and the journal holds one close-out for it, naming the handoff it wrote and holding the "
      "summary its session recorded, as written",
      len(written) == 1 and [type(entry) for entry in kept] == [Summary, Close]
      and (kept[1].session, kept[1].handoff, kept[1].summary) == (SESSION_ID, written[0], DID),
      f"written={written!r} journal={kept!r}")
shutil.rmtree(taken_dir, ignore_errors=True)
check("and the worker retires the old process once the launcher has returned",
      done.retired and "terminated" in done.worker_log, detail(done))
check("the successor's screen never shows the boot banner, and it still counts as taken",
      "fixture successor" in done.pane and "Claude Code v" not in done.pane and done.retired,
      f"pane={done.pane!r} " + detail(done))

for future, name, reason in ((silent, "records nothing", f"was not taken within {READY_TIMEOUT}s"),
                             (other, "records a different prompt", f"was not taken within {READY_TIMEOUT}s"),
                             (dies, "exits at boot", "claude exited with status 3 before"),
                             (killed, "is killed at boot", "claude exited on signal 15 before")):
    done = future.result()
    check(f"a successor that {name} is refused to the asking session, saying why",
          done.returncode == REFUSED_RC and reason in done.stderr and "NOT delivered" in done.stderr
          and "handoff delivered" not in done.stdout, detail(done))
    check(f"and it is torn down while the old process lives on ({name})",
          "kill-session" in done.tmux_log and not done.retired, detail(done))
done = silent.result()
check("the refusal carries the successor's last screen", "fixture successor" in done.stderr, detail(done))
done = dies.result()
check("a successor that exits at boot is refused with its last words, by its exit rather than the timeout",
      "unknown option '--bogus'" in done.stderr, detail(done))

for future, name in ((server_differs, "set"), (unset_here, "unset")):
    done = future.result()
    check(f"a tmux server's own CLAUDE_CONFIG_DIR cannot move the successor's transcript ({name} here)",
          "handoff delivered" in done.stdout and done.retired, detail(done))

done = stopped_then_taken.result()
check("a launcher stopped mid-wait still has its successor's fate decided: taken, the old process retires",
      done.retired, detail(done))
done = stopped_never_taken.result()
check("and never taken, the successor is torn down and the old process lives on",
      "kill-session" in done.tmux_log and not done.retired, detail(done))

# The host shim exits with its session claude's status, so 128 + the signal it died on.
done = hosted_taken.result()
check("a hosted session that hands off is stopped through its daemon once the launcher returns, never signalled",
      "handoff delivered" in done.stdout and done.returncode == 128 + signal.SIGUSR1
      and done.stop_log == f"stop {BACKGROUND_ID}\n" and "terminated" not in done.worker_log
      and f"the daemon stopped background session {BACKGROUND_ID}, and old claude" in done.worker_log,
      detail(done) + f" stop_log={done.stop_log!r}")


# --- the detached worker alone: what it retires, and in what order -------------

# A victim that ignores SIGTERM, so retirement has to escalate to KILL. The default
# `sleep 600` dies on the first signal and never reaches the KILL branch.
#
# The trailing `sleep 1` loop keeps the shell blocked without a long-lived child, so when KILL
# lands on the shell, the orphaned sleep exits within a second and the case leaves
# nothing behind.
IGNORES_TERM = ["/bin/bash", "-c", 'trap "" TERM; while :; do sleep 1; done']


def identity_of(pid):
    # `$(...)` strips trailing newlines and nothing else; matching that exactly
    # matters, because the whole check is a string comparison against it.
    return subprocess.run(["ps", "-o", "lstart=,ucomm=", "-p", str(pid)],
                          text=True, capture_output=True).stdout.rstrip("\n")


def worker_case(identity, goal="", fail_on=None, victim=("sleep", "600"), successor="records",
                hold_launcher=False, retirement=("signal",), stop_leaves=False):
    """Run one detached worker over a real process standing in for the old claude.

    Returns (retired, done): whether the victim was signalled, and the worker's
    result, where `done.tmux_log` lists every tmux argv the worker issued and
    `done.verdict` is what it sent the launcher on fd 3, read to EOF. identity="match" passes the victim's
    real identity, and identity="reused" swaps the command out, as happens when the OS
    reissues the pid. The launcher it waits on has already exited, unless
    `hold_launcher`, when it lives until the case has seen the victim survive it.
    `retirement` is the worker's trailing arguments, and the fixture daemon holds the victim
    as background session BACKGROUND_ID, so `done.victim_signal` tells its stop (SIGUSR1)
    from a TERM or KILL; `stop_leaves` has the daemon accept the stop and leave the victim
    running. The victim is reaped the moment it exits, as a claude is by the process that
    started it, so ps never shows it lingering as a zombie.
    """
    proc = subprocess.Popen(list(victim), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=proc.wait, daemon=True).start()
    real = identity_of(proc.pid)
    if identity == "match":
        ident = real
    else:
        ident = real.replace(os.path.basename(victim[0]), "vi")
        # A substitution that matches nothing would leave the real identity under the name
        # "reused", and the case would pass while testing the opposite.
        assert ident != real, f"reused-identity fixture altered nothing: {real!r}"
    launcher = subprocess.Popen(["sleep", "600" if hold_launcher else "0"])
    launcher_ident = identity_of(launcher.pid)
    if not hold_launcher:
        launcher.wait()
    workdir = tempfile.mkdtemp(prefix="finalize-worker.")
    config = os.path.join(workdir, "config")
    msgfile = os.path.join(workdir, "msg")
    goalfile = os.path.join(workdir, "goal")
    with open(msgfile, "w") as handle:
        handle.write(f"handoff\n\nThis handoff verbatim on disk: {msgfile}")
    with open(goalfile, "w") as handle:
        handle.write(goal)
    successor_id = "0b6f2d1e-7c3a-4e9b-8d5f-2a1c3e4b5d6f"
    relaunch = (f"env CLAUDE_CONFIG_DIR={shlex.quote(config)} {shlex.quote(SUCCESSOR_BIN)} "
                f'--session-id {successor_id} "$(cat {shlex.quote(msgfile)})"')
    tmux_state = os.path.join(workdir, "tmux")
    tmuxlog = os.path.join(workdir, "tmux.log")
    env = {
        "PATH": ":".join([FIXTURES, REAL_DIRS]),
        "HOME": HOME,
        "TMPDIR": workdir,
        "FIXTURE_TMUX_LOG": tmuxlog,
        "FIXTURE_TMUX_STATE": tmux_state,
        "FIXTURE_SUCCESSOR": successor,
        "FIXTURE_BACKGROUND_ID": BACKGROUND_ID,
        "FIXTURE_BACKGROUND_PID": os.path.join(workdir, "victim.pid"),
    }
    with open(env["FIXTURE_BACKGROUND_PID"], "w") as handle:
        handle.write(str(proc.pid))
    if stop_leaves:
        env["FIXTURE_STOP_LEAVES"] = "1"
    if fail_on is not None:
        env["FIXTURE_TMUX_FAIL"] = fail_on
    verdict_read, verdict_write = os.pipe()
    try:
        # The pipe is moved onto fd 3, where the launcher's substitution puts it. Each move is
        # its own exec: `exec cmd 3>&10 10>&-` leaves bash's saved copy of fd 10 open in cmd.
        worker = subprocess.Popen(
            ["/bin/bash", "-c", f'exec 3>&{verdict_write}; exec {verdict_write}>&-; exec "$@"', "bash",
             LAUNCHER, "--detached-worker", "fixture-session", workdir, relaunch, config, successor_id,
             msgfile, goalfile, str(proc.pid), ident, str(launcher.pid), launcher_ident,
             *retirement],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, pass_fds=(verdict_write,))
        os.close(verdict_write)
        # The verdict is whatever came before EOF. A process still holding the pipe after the
        # worker is done (a tmux server it started) would keep the launcher waiting, so a
        # read that outlasts the worker by 5s fails the case.
        verdict_box = {}
        reader = threading.Thread(target=lambda: verdict_box.update(text=os.fdopen(verdict_read).read()), daemon=True)
        reader.start()
        survived_launcher = None
        if hold_launcher:
            reader.join(40)
            time.sleep(1.5)
            survived_launcher = proc.poll() is None
            launcher.kill()
            launcher.wait()
        out, err = worker.communicate(timeout=180)
        reader.join(5)
        done = subprocess.CompletedProcess(worker.args, worker.returncode, out, err)
        try:
            proc.wait(timeout=5)
            retired = True
        except subprocess.TimeoutExpired:
            retired = False
        done.victim_signal = -proc.returncode if retired and proc.returncode < 0 else None
        done.tmux_log = open(tmuxlog).read() if os.path.exists(tmuxlog) else ""
        done.verdict = verdict_box.get("text", "(the pipe was still held open after the worker exited)")
        done.survived_launcher = survived_launcher
        return retired, done
    finally:
        proc.kill()
        proc.wait()
        launcher.kill()
        launcher.wait()
        try:
            with open(os.path.join(tmux_state, "pid")) as handle:
                successor_pid = handle.read().strip()
            subprocess.run(["pkill", "-KILL", "-P", successor_pid], capture_output=True)
            subprocess.run(["kill", "-KILL", successor_pid], capture_output=True)
        except FileNotFoundError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    same_pid = pool.submit(worker_case, "match")
    reused_pid = pool.submit(worker_case, "reused")
    goal_lands = pool.submit(worker_case, "match", goal="ship the thing")
    # The goal delivery is staged to fail mid-way, at the buffer it pastes from.
    goal_fails = pool.submit(worker_case, "match", goal="ship the thing", fail_on="load-buffer")
    spawn_fails = pool.submit(worker_case, "match", fail_on="new-session")
    # A victim that ignores TERM, which is the only way to reach the KILL escalation.
    survives_term = pool.submit(worker_case, "match", victim=IGNORES_TERM)
    launcher_held = pool.submit(worker_case, "match", hold_launcher=True)
    stopped = pool.submit(worker_case, "match", retirement=("stop", HOST_SHIM, BACKGROUND_ID))
    stop_refused = pool.submit(worker_case, "match", retirement=("stop", HOST_SHIM, "ffffffff"))
    stop_leaves = pool.submit(worker_case, "match", retirement=("stop", HOST_SHIM, BACKGROUND_ID),
                              stop_leaves=True)
    no_retirement = pool.submit(worker_case, "match", retirement=("stop", HOST_SHIM))
    stop_after_gone = pool.submit(worker_case, "reused", retirement=("stop", HOST_SHIM, BACKGROUND_ID))


def worker_detail(done):
    return f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r} verdict={done.verdict!r}"


retired, done = same_pid.result()
check("the worker retires the old process it was given, and tells the launcher it was taken",
      retired and done.verdict == "taken\n", worker_detail(done))

# The window between capturing the pid and signalling it spans the successor's boot,
# which is time enough for the OS to reissue the pid. `kill -0` cannot detect that,
# since it reports only that something is alive.
retired, done = reused_pid.result()
check("a pid reissued to another process during the wait is not signalled",
      not retired, worker_detail(done))
check("and the worker says why it left that pid alone",
      "not the process captured at launch" in done.stdout, worker_detail(done))

# The goal goes in while the successor's first turn runs, before the retirement, which takes up to ~20s
# through a daemon: after it, the goal could land in a successor that has already handed off.
def before_retirement(log, line):
    return line in log and "terminated" in log and log.index(line) < log.index("terminated")


retired, done = goal_lands.result()
check("the detached worker types the carried goal before it retires the old process",
      retired and "buffer: /goal ship the thing" in done.tmux_log
      and before_retirement(done.stdout, "re-establishing carried goal"),
      worker_detail(done) + f" tmux_log={done.tmux_log!r}")

# A goal delivery that fails is logged and handled, so the retirement after it still runs.
retired, done = goal_fails.result()
check("a failed goal delivery cannot leave the old process alive", retired, worker_detail(done))
check("and the failure is logged, with the goal to deliver by hand, not passed off as delivered",
      before_retirement(done.stdout, "ERROR: could not re-establish the carried goal")
      and "/goal ship the thing" in done.stdout and "paste-buffer" not in done.tmux_log,
      worker_detail(done) + f" tmux_log={done.tmux_log!r}")

# A successor that cannot be spawned at all. The old process is the only agent, so it
# must still run, and the launcher is told so.
retired, done = spawn_fails.result()
check("a session that cannot be created leaves the old process running",
      not retired, worker_detail(done))
check("and the failed spawn is a refused verdict, not a quiet no-op",
      done.returncode == 1 and done.verdict.startswith("refused\ncould not create detached tmux session"),
      worker_detail(done))
# Nothing was created, so nothing is torn down.
check("a spawn that never happened tears nothing down",
      "kill-session" not in done.tmux_log, f"tmux_log={done.tmux_log!r}")

retired, done = survives_term.result()
check("a process that refuses TERM is still retired, by escalation to KILL",
      retired, worker_detail(done))
check("and the escalation says so, rather than the pid quietly disappearing",
      "survived TERM; sending KILL" in done.stdout, worker_detail(done))

# The old process's claude would tear down the launcher's Bash call when it dies, so
# the worker retires it only once the launcher has returned.
retired, done = launcher_held.result()
check("the worker does not retire the old process while the launcher is still running",
      done.survived_launcher is True and retired, worker_detail(done))

# A daemon respawns a background session whose process exits on a signal, and prompts it to
# continue beside its successor, so a background session is retired through `claude stop`.
retired, done = same_pid.result()
check("a session retired by signal is never stopped through a daemon",
      done.victim_signal == signal.SIGTERM and "asking the daemon" not in done.stdout, worker_detail(done))
retired, done = stopped.result()
check("a background session is retired by its daemon's stop, not by a signal",
      done.verdict == "taken\n" and done.victim_signal == signal.SIGUSR1
      and f"the daemon stopped background session {BACKGROUND_ID}, and old claude {done.args[13]} no longer runs" in done.stdout
      and f"(old claude {done.args[13]}: still running)" in done.stdout, worker_detail(done))
retired, done = stop_after_gone.result()
check("a background session whose old process has already gone is still stopped by its id, saying so",
      done.victim_signal == signal.SIGUSR1 and f"(old claude {done.args[13]}: already gone" in done.stdout,
      worker_detail(done))
retired, done = stop_refused.result()
check("a stop the daemon refuses leaves the old process unsignalled, and the log says it is still live",
      not retired and "ERROR: the daemon did not stop background session ffffffff:" in done.stdout
      and "stop ffffffff' exited 1: No job matching ffffffff" in done.stdout
      and "still live beside its successor" in done.stdout, worker_detail(done))
retired, done = stop_leaves.result()
check("a stop the daemon accepts that leaves the process running is never escalated to a signal",
      not retired and "still runs 10s later" in done.stdout and "KILL" not in done.stdout, worker_detail(done))
retired, done = no_retirement.result()
check("a retirement the worker cannot parse is refused before anything is started",
      done.returncode == 2 and "retirement is 'signal' or 'stop <claude>" in done.stderr
      and done.tmux_log == "" and not retired, worker_detail(done) + f" tmux_log={done.tmux_log!r}")


# ---------------------------------------------------------------------------
# The handoff is durable, and every delivery names it.
#
# A handoff that exists only in flight is lost on the first failed paste. These cases
# check that the payload is on disk before any transport is chosen, that no code path
# removes it, and that the message carries its own path.
# ---------------------------------------------------------------------------

def tmux_worker_case(pane_text, timeout="2", settings=(), goal=""):
    """Run one tmux worker and report what it did with the handoff.

    Returns (done, whether the handoff file still exists, tmux log). `pane_text` is
    what capture-pane shows, so a reset that never registers is a value, not a mode.
    `goal` is the carried goal, "" for none.
    """
    workdir = tempfile.mkdtemp(prefix="finalize-tmux.")
    msgfile = os.path.join(workdir, "handoff.md")
    goalfile = os.path.join(workdir, "goal")
    with open(msgfile, "w") as handle:
        handle.write("the whole handoff")
    with open(goalfile, "w") as handle:
        handle.write(goal)
    tmuxlog = os.path.join(workdir, "tmux.log")
    env = {
        "PATH": ":".join([FIXTURES, REAL_DIRS]),
        "HOME": HOME,
        "TMPDIR": workdir,
        "FIXTURE_TMUX_LOG": tmuxlog,
        "FIXTURE_TMUX_PANE_TEXT": pane_text,
        "MEMENTO_RESET_TIMEOUT_SECONDS": timeout,
    }
    try:
        done = subprocess.run(
            [LAUNCHER, "--worker", "%1", msgfile, goalfile, *settings],
            text=True, capture_output=True, env=env, timeout=180)
        log = open(tmuxlog).read() if os.path.exists(tmuxlog) else ""
        return done, os.path.exists(msgfile), log
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    registered = pool.submit(tmux_worker_case, "Claude Code v1.2.3")
    never = pool.submit(tmux_worker_case, "still working on the previous turn")
    goal_registered = pool.submit(tmux_worker_case, "Claude Code v1.2.3", goal="ship it")
    goal_never = pool.submit(tmux_worker_case, "still working on the previous turn", goal="ship it")

done, survived, log = registered.result()
check("a reset the worker can see delivers the handoff",
      "buffer: the whole handoff" in log, f"out={done.stdout!r} log={log!r}")
check("and the handoff file outlives the worker that delivered it",
      survived, f"out={done.stdout!r}")

# The reset can be typed while the agent is still mid-turn, so Claude queues it and the
# banner may not appear within any fixed window. Tmux type-ahead preserves ordering, so
# the worker sends regardless and never deletes a handoff it could not confirm.
done, survived, log = never.result()
check("a reset the worker never sees still delivers the handoff",
      "buffer: the whole handoff" in log, f"out={done.stdout!r} log={log!r}")
check("and says plainly that it could not confirm the reset",
      "not observed" in done.stdout, f"out={done.stdout!r}")
check("and never claims the handoff was withheld",
      "NOT sent" not in done.stdout and "misfired" not in log,
      f"out={done.stdout!r} log={log!r}")
check("and above all does not delete the handoff it could not confirm",
      survived, f"out={done.stdout!r}")

# /goal is an immediate command, so it does not queue behind a /clear the way the handoff does: typed
# before the reset has run, it would be set in the old context and wiped by the reset. It is typed after
# the handoff once the reset is seen, and otherwise not at all, with the goal named for delivery by hand.
done, _, log = goal_registered.result()
check("a reset the worker can see has the carried goal typed after the handoff",
      0 <= log.find("buffer: the whole handoff") < log.find("buffer: /goal ship it"),
      f"out={done.stdout!r} log={log!r}")
done, _, log = goal_never.result()
check("a reset the worker never sees has no goal typed, and the goal named to deliver by hand",
      "/goal" not in log and "carried goal NOT delivered" in done.stdout and "/goal ship it" in done.stdout,
      f"out={done.stdout!r} log={log!r}")

# The requested model and effort are typed into the reset session before the handoff is
# pasted, so the handoff's first turn already runs on them.
done, _, log = tmux_worker_case("Claude Code v1.2.3", settings=("/model opus", "/effort high"))
model_at, effort_at, paste_at = (log.find(needle) for needle in
                                 ("buffer: /model opus", "buffer: /effort high", "buffer: the whole handoff"))
check("the worker types the requested /model and /effort, in order, before the handoff",
      0 <= model_at < effort_at < paste_at, f"out={done.stdout!r} log={log!r}")
done, _, log = tmux_worker_case("Claude Code v1.2.3")
check("with nothing requested the worker types no setting",
      "buffer: /model" not in log and "buffer: /effort" not in log and "buffer: the whole handoff" in log,
      f"out={done.stdout!r} log={log!r}")

# The launcher's half: the payload lands in its durable home and names itself.
handoff_home = tempfile.mkdtemp(prefix="finalize-home.")
try:
    done = run(message="carry this forward", handoff_dir=handoff_home)
    written = [os.path.join(handoff_home, f) for f in os.listdir(handoff_home)]
    check("the launcher writes the handoff into its durable home",
          len(written) == 1, f"found={written} out={done.stdout!r}")
    body = open(written[0]).read() if len(written) == 1 else ""
    check("the handoff on disk carries the agent's message",
          "carry this forward" in body, f"body={body!r}")
    check("and names its own path, so a mangled copy is still recoverable",
          written and written[0] in body, f"body={body!r}")
finally:
    shutil.rmtree(handoff_home, ignore_errors=True)

STRANGER.terminate()
STRANGER.wait()
# --- a handoff hands off: record and reset are one act ----------------------------------
# There is no flag that records a handoff without resetting the session. Every run picks
# a transport after the message is on disk.

done = run()
check("every run resets: a transport is chosen with no flag asked for",
      "transport=" in done.stdout and "handoff recorded" not in done.stdout, done.stdout[:200])
# The removed flag, in the value-carrying forms callers used to pass. A bare `--reset`
# is refused either way, so it would prove nothing.
for mode in ("clear", "compact"):
    done, bodies = argv_case("--reset", mode, "real work goes here")
    check(f"--reset {mode} is refused as the unknown flag it now is",
          done.returncode == REFUSED_RC and "unknown flag '--reset'" in done.stderr
          and bodies == [], f"rc={done.returncode} err={done.stderr!r} handoffs={bodies!r}")

handoffs = tempfile.mkdtemp(prefix="finalize-recorded.")
try:
    run(handoff_dir=handoffs, message="carry this forward")
    written = [os.path.join(handoffs, n) for n in os.listdir(handoffs)]
    check("the handoff is on disk before the transport runs", len(written) == 1, str(written))
    check("and carries the message",
          written and "carry this forward" in open(written[0]).read(), str(written))
finally:
    shutil.rmtree(handoffs, ignore_errors=True)

# --- the close-out is journaled before the handoff has its name -------------------------
# A real run journals its session's close-out, holding the summary the session recorded, and
# only then gives the handoff its name. These run the launcher for real with no claude above
# it: the journal is asked before any transport is looked for, and a run whose close-out is
# recorded is then refused for the unset $CLAUDE_PID, so nothing is spawned.


def close_case():
    """A directory of a case's own, and the journal in it."""
    case = tempfile.mkdtemp(prefix="finalize-close.")
    return case, os.path.join(case, "journal.jsonl")


def closing(case, session_id=SESSION_ID, handoff_dir=None):
    """A real launcher run from `case`, against the journal and the handoff directory in it.
    Returns the completed process and the files in the handoff directory afterwards."""
    handoffs = os.path.join(case, "handoffs")
    env = {"PATH": REAL_DIRS, "HOME": case, "TMPDIR": case,
           "MEMENTO_HANDOFF_DIR": handoff_dir or handoffs,
           "MEMENTO_JOURNAL": os.path.join(case, "journal.jsonl")}
    if session_id is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    done = subprocess.run([LAUNCHER, "a real handoff"], text=True, capture_output=True, timeout=30,
                          env=env, cwd=case)
    names = sorted(os.listdir(handoffs)) if os.path.isdir(handoffs) else []
    return done, [os.path.join(handoffs, name) for name in names]


AFTER_CLOSE = "$CLAUDE_PID is unset"
NAMES_JOURNAL = f"The journal's command is: {JOURNAL_CLI}"

case, journal = close_case()
summarize(journal, f"\n{DID}\n\n\n")
done, written = closing(case)
kept = entries(journal)
check("a real run journals one close-out, naming the handoff it wrote and holding the summary",
      len(written) == 1 and [type(entry) for entry in kept] == [Summary, Close]
      and (kept[1].session, kept[1].handoff, kept[1].summary) == (SESSION_ID, written[0], DID),
      f"err={done.stderr!r} written={written!r} journal={kept!r}")
check("and a run refused after its close-out names that handoff as preserved",
      done.returncode == REFUSED_RC and AFTER_CLOSE in done.stderr
      and f"preserved at: {written[0]}" in done.stderr, f"rc={done.returncode} err={done.stderr!r}")
# That close-out took the summary, so the same command again is the journal's to refuse.
done, again = closing(case)
check("the same run again is refused with the journal's reason and its command, and writes nothing",
      done.returncode == REFUSED_RC and "since its last close-out" in done.stderr
      and NAMES_JOURNAL in done.stderr and "preserved at" not in done.stderr
      and again == written and entries(journal) == kept,
      f"rc={done.returncode} err={done.stderr!r} handoffs={again!r}")
summarize(journal, "tried again")
done, again = closing(case)
check("and with a summary recorded again it gets as far as it did before",
      AFTER_CLOSE in done.stderr and len(again) == 2
      and entries(journal)[-1].summary == "tried again",
      f"rc={done.returncode} err={done.stderr!r} handoffs={again!r}")
shutil.rmtree(case, ignore_errors=True)

# The handoff a real run writes is the prompt its successor wakes with, and the wake hook knows a
# handoff by the lines this launcher frames it with. The hook is given that handoff here, in the
# suite a change to the framing is run against: the wake it journals leads back to this run's
# close-out, which is all a later `journal recall` follows.
case, journal = close_case()
summarize(journal, DID)
done, written = closing(case)
WOKEN_ID = "0c4e7d1a-9b2f-4a6c-8e3d-7f5a2b9c1d60"
woken = subprocess.run(
    [WAKE_HOOK], text=True, capture_output=True,
    input=json.dumps({"session_id": WOKEN_ID, "hook_event_name": "UserPromptSubmit", "cwd": case,
                      "prompt": open(written[0]).read() if written else ""}),
    env={**os.environ, "MEMENTO_JOURNAL": journal,
         "MEMENTO_CEILING_LOG": os.path.join(case, "hook.log")})
kept = entries(journal)
links, ending = chain(kept, WOKEN_ID)
check("the handoff a real run writes wakes a session whose chain leads back to that run's close-out",
      woken.returncode == 0 and woken.stdout == "" and len(written) == 1
      and [kept[at] for at in links] == [entry for entry in kept if isinstance(entry, Close)]
      and [(entry.session, entry.handoff) for entry in kept if isinstance(entry, Close)]
      == [(SESSION_ID, written[0])],
      f"rc={woken.returncode} out={woken.stdout!r} err={woken.stderr!r} journal={kept!r} "
      f"ending={ending!r}")
shutil.rmtree(case, ignore_errors=True)


def spoil(journal):
    with open(journal, "ab") as record:
        record.write(b"not an entry\n")


def unspoil(journal):
    with open(journal, "rb") as record:
        whole = record.read()
    with open(journal, "wb") as record:
        record.write(whole.replace(b"not an entry\n", b""))


# Every way the journal does not record the close-out is one refusal: the journal's reason, its
# command, and no file in the handoff directory, under a handoff's name or any other. The
# record is left as it was, so the summary is there for the run that follows the repair.
for what, summarized, broken, reason, mended in (
        ("no summary recorded", False, lambda journal: None, "has recorded no summary",
         lambda journal: summarize(journal, "handed off")),
        ("a journal with a line that is not an entry", True, spoil,
         "journal.jsonl line 2 is not a journal entry", unspoil),
        ("a journal that cannot be written", True, lambda journal: os.chmod(journal, 0o400),
         "Permission denied", lambda journal: os.chmod(journal, 0o600))):
    case, journal = close_case()
    if summarized:
        summarize(journal, "handed off")
    broken(journal)
    before = open(journal, "rb").read() if os.path.exists(journal) else None
    done, written = closing(case)
    after = open(journal, "rb").read() if os.path.exists(journal) else None
    check(f"{what} is refused with the journal's reason and its command, and no handoff is written",
          done.returncode == REFUSED_RC and reason in done.stderr and NAMES_JOURNAL in done.stderr
          and "preserved at" not in done.stderr and written == [] and after == before,
          f"rc={done.returncode} err={done.stderr!r} handoffs={written!r}")
    mended(journal)
    done, written = closing(case)
    last = entries(journal)[-1]
    check(f"and once that is put right the run closes out with the summary ({what})",
          AFTER_CLOSE in done.stderr and len(written) == 1 and isinstance(last, Close)
          and (last.handoff, last.summary) == (written[0], "handed off"),
          f"rc={done.returncode} err={done.stderr!r} handoffs={written!r} last={last!r}")
    shutil.rmtree(case, ignore_errors=True)

# A handoff directory given relative to the launcher's own is the launcher's to make whole: the
# close-out and the handoff's last line both name the file by its full path, the one path a wake
# can find the close-out by from any directory.
case, journal = close_case()
summarize(journal, "handed off")
done, written = closing(case, handoff_dir="handoffs")
last = entries(journal)[-1]
check("a handoff directory given as a relative path is closed out and named by its full path",
      AFTER_CLOSE in done.stderr and len(written) == 1 and isinstance(last, Close)
      and os.path.isabs(last.handoff) and os.path.samefile(last.handoff, written[0])
      and open(written[0]).read().endswith(f"This handoff verbatim on disk: {last.handoff}"),
      f"rc={done.returncode} err={done.stderr!r} handoffs={written!r} last={last!r}")
shutil.rmtree(case, ignore_errors=True)

# A run with no session id hands off on the pane transport, so its close-out must not fail for
# the id: it holds the summary recorded the same way, and never a named session's.
case, journal = close_case()
summarize(journal, "did it unnamed", session_id=None)
summarize(journal, "did it named")
done, written = closing(case, session_id=None)
last = entries(journal)[-1]
check("a run whose environment names no session closes out with the summary recorded with none",
      AFTER_CLOSE in done.stderr and len(written) == 1 and isinstance(last, Close)
      and (last.session, last.handoff, last.summary) == (None, written[0], "did it unnamed"),
      f"rc={done.returncode} err={done.stderr!r} last={last!r}")
shutil.rmtree(case, ignore_errors=True)

# A dry run and --help journal nothing and ask the journal nothing, so neither needs a summary.
case, journal = close_case()
summarize(journal, "another session's", session_id="another-session")
before = open(journal, "rb").read()
for what, flags_given, said in (("a dry run", (), "[dry-run] close-out not journaled"),
                                ("--help", ("--help",), "usage: finalize-session")):
    done = run(journal=journal, launcher_flags=flags_given)
    check(f"{what} with no summary recorded exits 0 and leaves the journal as it was",
          done.returncode == 0 and said in done.stdout and open(journal, "rb").read() == before,
          f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
shutil.rmtree(case, ignore_errors=True)

# The two commands of a close-out are run as one, and the hook respells the whole of a command
# that names the launcher. The summary's text is in a file and not in the command, so the
# journal holds it as written, where the message is respelled. The commands are found on PATH
# and the file by a relative name, so the command sent to the hook is the same on every machine.
case, journal = close_case()
with open(os.path.join(case, "closeout.md"), "w") as handle:
    handle.write("Ran git pull, then bash -n.\n")
asked = {"tool_name": "Bash", "tool_input": {
    "command": "journal summary --file closeout.md && finalize-session 'Ran git pull, then bash -n.'"}}
sent = json.loads(subprocess.run([HOOK], input=json.dumps(asked), text=True, capture_output=True,
                                 check=True).stdout)["hookSpecificOutput"]["updatedInput"]["command"]
rootfile = os.path.join(case, "root.pid")
with open(rootfile, "w") as handle:
    handle.write(str(os.getpid()))
# The launcher runs under a claude this runner's pane shows, as in `argv_case`.
done = subprocess.run(
    [CLAUDE_BIN, "-c", 'export CLAUDE_PID=$$; eval "$1"; exit $?', "wrap", sent],
    text=True, capture_output=True, timeout=30, cwd=case,
    env={"PATH": ":".join((JOURNAL_BIN, os.path.realpath(HERE), FIXTURES, REAL_DIRS)),
         "HOME": case, "TMPDIR": case, "MEMENTO_HANDOFF_DIR": os.path.join(case, "handoffs"),
         "MEMENTO_JOURNAL": journal, "CLAUDE_CODE_SESSION_ID": SESSION_ID,
         "FINALIZE_DRY_RUN": "1", "FIXTURE_ROOT_PID": rootfile, "FIXTURE_PANES": "%99 ROOTPID 0"})
bodies = [open(os.path.join(case, "handoffs", name)).read()
          for name in (os.listdir(os.path.join(case, "handoffs")) if done.returncode == 0 else [])]
kept = entries(journal)
check("a summary and a handoff sent as one command: the message is respelled and the summary is not",
      done.returncode == 0 and "finalize-session 'Ran g-i-t pull, then b-a-s-h -n.'" in sent
      and len(bodies) == 1 and "\nRan g-i-t pull, then b-a-s-h -n.\n" in bodies[0]
      and [type(entry) for entry in kept] == [Summary]
      and kept[0].summary == "Ran git pull, then bash -n.",
      f"sent={sent!r} rc={done.returncode} err={done.stderr!r} bodies={bodies!r} journal={kept!r}")
shutil.rmtree(case, ignore_errors=True)

shutil.rmtree(FIXTURES, ignore_errors=True)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
