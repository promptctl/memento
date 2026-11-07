#!/usr/bin/env python3
"""Tests for finalize-session's tmux pane discovery.

Each case runs the launcher as an agent does (argv in, `--dry-run` report out)
and asserts which transport and pane it selected. The process tree is real:
`nest` builds a chain of genuine processes above the launcher, so the walk runs
against real ps output, pids, and elapsed times. tmux is the only fixture, and the
ps wrapper in the pid-reuse case forges one ancestor's age while delegating
everything else to the real ps. The detached-transport cases plant a process
named `claude` in that chain, so the claude lookup finds a process this suite
owns rather than whatever claude runs on the host.

Run: python3 finalize-session.test.py
"""

import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(HERE, "finalize-session")
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
# display-message echoes a target naming the pane it was asked about, so an
# assertion can see which pane won.
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
  display-message)
    # Real tmux does NOT validate -t here: for a pane no server owns it exits 0
    # with every field empty, so the launcher's format renders as the bare ":."
    # - non-empty, and a live tmux address for the CURRENT pane at that. Modelling
    # this as a failure would be the comfortable lie; it is what let a stale
    # $TMUX_PANE retarget the handoff unnoticed. Known panes echo their id and a
    # target naming them, so an assertion can see which pane won.
    pane=""; fmt=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -t) pane="${2:-}"; shift 2 ;;
        -p) shift ;;
        *)  fmt="$1"; shift ;;
      esac
    done
    # ':.' is not a pane id but a target expression meaning current-session:
    # current-window.current-pane, and tmux resolves it to a REAL pane - which is
    # why an unresolvable id rendering into ':.' was dangerous rather than merely
    # wrong. The first live record stands in for "current" here.
    known=""; pane_dead=""; resolved=""
    while read -r id pid dead; do
      [ -n "$id" ] || continue
      [ "$id" = "$pane" ] && { known=yes; pane_dead="$dead"; resolved="$id"; }
      [ "$pane" = ":." ] && [ "$dead" = 0 ] && [ -z "$resolved" ] \
        && { known=yes; pane_dead="$dead"; resolved="$id"; }
    done <<< "${FIXTURE_PANES:-}"
    pane="$resolved"
    if [ -n "$known" ]; then
      line="${fmt//\#\{pane_id\}/$pane}"
      line="${line//\#\{pane_dead\}/$pane_dead}"
      line="${line//\#\{session_name\}/target-for-$pane}"
      line="${line//\#\{window_index\}/0}"
      line="${line//\#\{pane_index\}/0}"
    else
      # Every field empty, exit 0 - tmux's actual answer for a pane it does not
      # own, and the reason ":." reaches the launcher looking like a target.
      line="${fmt//\#\{pane_id\}/}"
      line="${line//\#\{pane_dead\}/}"
      line="${line//\#\{session_name\}/}"
      line="${line//\#\{window_index\}/}"
      line="${line//\#\{pane_index\}/}"
    fi
    case "$line" in
      *'#{'*) echo "fixture tmux: cannot expand '$fmt'" >&2; exit 2 ;;
    esac
    printf '%s\n' "$line"
    ;;
  new-session|has-session|capture-pane|load-buffer|paste-buffer|send-keys|kill-session)
    # The detached WORKER's half of the fixture. It needs almost no state: the
    # worker only ever asks whether the new session is up and whether its banner
    # is drawn, so a session that is always up plus one value for what its pane
    # shows is the whole model.
    # $FIXTURE_TMUX_FAIL names one subcommand to fail instead, which is how a
    # goal delivery that dies mid-way - or a session that cannot be created at all
    # - is staged. $FIXTURE_TMUX_PANE_TEXT is what capture-pane shows: a session
    # that never finishes booting is that same always-up session with a different
    # value in this one field, not a second mode.
    # Every subcommand appends its whole argv to $FIXTURE_TMUX_LOG, which is how a
    # case can see that a teardown the worker owes actually happened - a log line
    # from the worker only says it meant to.
    sub="$1"
    if [ "${FIXTURE_TMUX_FAIL:-}" = "$sub" ]; then
      echo "fixture tmux: failing '$sub' on request" >&2
      exit 1
    fi
    printf '%s\n' "$*" >> "${FIXTURE_TMUX_LOG:-/dev/null}"
    [ "$sub" = capture-pane ] && printf '%s\n' "${FIXTURE_TMUX_PANE_TEXT:-Claude Code v1.2.3}"
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
self="${NEST_SELF:-$0}"
if [ -n "${NEST_PUBLISH_PID:-}" ]; then
  printf '%s' "$$" > "$NEST_PUBLISH_PID"
  unset NEST_PUBLISH_PID
fi
# The hop named `claude`, planted so the launcher's ancestry walk has a claude
# process of this fixture's own making to find. Re-exec rather than fork: the pid
# is unchanged, so the pid already published above IS this claude - which is what
# lets an assertion name the exact process the launcher was supposed to find. The
# name is the whole of the fixture, because the launcher matches on argv0, and
# `exec -a` is what puts a name of this fixture's choosing there - a script run
# through its shebang is argv0 `/bin/bash` however it is symlinked. Everything
# below this hop proceeds as ordinary nest.
# $NEST_CLAUDE_AT may name several depths; each is planted once. The pid of the
# last one planted (the deepest) goes to $NEST_RECORD_CLAUDE, so a case can name
# the exact claude it expects the launcher to pick.
case " ${NEST_CLAUDE_AT:-} " in
  *" $depth "*)
    NEST_CLAUDE_AT=" $NEST_CLAUDE_AT "; NEST_CLAUDE_AT="${NEST_CLAUDE_AT// $depth / }"
    export NEST_CLAUDE_AT
    [ -n "${NEST_RECORD_CLAUDE:-}" ] && printf '%s' "$$" > "$NEST_RECORD_CLAUDE"
    exec -a "$NEST_AS_CLAUDE" /bin/bash "$self" "$depth" "$@" ;;
esac
if [ "$depth" -gt 0 ]; then
  if [ "${NEST_REHOST_AT:-}" = "$depth" ]; then
    unset NEST_REHOST_AT
    # argv0 is the host's own spelling, so the launcher reads this hop as a real
    # `claude --bg-pty-host` would read; a shebang script alone is argv0 /bin/bash.
    (exec -a "claude --bg-pty-host" /bin/bash "$(command -v claude)" daemon run --bg-pty-host -- "$self" $((depth - 1)) "$@")
    exit $?
  fi
  "$self" $((depth - 1)) "$@"
  exit $?
fi
# $NEST_SLEEP holds the bottom of the chain still long enough that ps reports a
# non-zero elapsed time for it. Without that the whole chain is born inside one
# second, every age reads 00:00, and no forged age can be younger than its own
# descendant - so the age check would have nothing to catch.
[ -n "${NEST_SLEEP:-}" ] && sleep "$NEST_SLEEP"
"$@"
exit $?
"""

CLAUDE = r"""#!/bin/bash
# Stands in for `claude daemon run --bg-pty-host`: the daemon host. A session it
# hosts is not displayed by any pane above it, and by default inherits none of
# tmux's environment - stripped here for real, not simulated.
set -uo pipefail
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

PS_FORGING = r"""#!/bin/bash
# The real process table with one ancestor's elapsed time rewritten to
# $FIXTURE_FORGE_AGE - a pid younger than the descendant claiming it is the
# signature of a recycled pid, and the one thing no live machine will produce on
# cue.
#
# The age is a parameter rather than a constant so the same wrapper can also
# forge an age that is OLDER, which the walk must still accept. That pairing is
# the control: both runs drive this identical rewrite of $3 for the identical
# pid and differ only in the value written, so a wrapper that mangled `ps -eo`
# into an unparseable table would fail the accepting run instead of quietly
# handing the refusing one a pass it did not earn. A control that skips this
# wrapper entirely - as the first version did - cannot see that failure mode at
# all, because it never runs the thing it is controlling for.
#
# Only the whole-table form is forged. A per-pid query carries no pid column to
# key on, so rewriting its fields would corrupt the launcher's answer instead of
# forging an age. Anything else passes straight through.
set -uo pipefail
out=$(/bin/ps "$@") || exit $?
case "${1:-}" in
  -e*) printf '%s\n' "$out" \
         | /usr/bin/awk -v forge="$(cat "$FIXTURE_ROOT_PID")" \
                        -v age="$FIXTURE_FORGE_AGE" \
             '$1 == forge { $3 = age } { print }' ;;
  *)   printf '%s\n' "$out" ;;
esac
"""


PS_FORGE_COMMAND = r"""#!/bin/bash
# The real ps, except that the planted claude's command line reads as
# $FIXTURE_FORGE_COMMAND. A daemon-hosted session is launched with a dozen flags
# the fixture chain cannot carry in its own argv - `nest` owns every token after
# argv0 - so the one read that asks what the pid was launched with is answered
# with the shape the field reports. Only the `-o command= -p <pid>` form, and only
# for that pid; the identity read and every ancestry hop pass through untouched.
set -uo pipefail
if [ "${1:-}" = "-o" ] && [ "${2:-}" = "command=" ] && [ "${3:-}" = "-p" ] \
   && [ "${4:-}" = "$(cat "$FIXTURE_ROOT_PID")" ]; then
  printf '%s\n' "$FIXTURE_FORGE_COMMAND"
  exit 0
fi
exec /bin/ps "$@"
"""


PS_MUTE_IDENTITY = r"""#!/bin/bash
# The real ps everywhere except the one query that asks what a pid is HOLDING:
# `ps -o lstart=,command= -p <pid>` exits 0 and prints nothing, which is what a
# pid whose holder exited between the lookup and the query actually reports. It
# is the one answer a bare exit-code check cannot see — success, with no identity
# in it — and the launcher must read it as "I could not identify this process",
# never as an identity of its own.
#
# Selectivity is keyed on the field list, not on the subcommand shape, because
# `-o command=` and `-o ppid=` (the ancestry hops) and `-eo pid=,ppid=,etime=`
# (the whole-table walk) are the same `-p`/`-o` grammar and must all pass
# through untouched. Muting them instead would collapse discovery for a reason
# having nothing to do with the identity read, and the case would pass on the
# wrong evidence. The control below runs under this same wrapper and still
# resolves a pane, which is what proves the pass-through is real.
set -uo pipefail
for arg in "$@"; do
  [ "$arg" = "lstart=,command=" ] && exit 0
done
exec /bin/ps "$@"
"""


def install(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write(body)
    os.chmod(path, 0o755)
    return path


FIXTURES = tempfile.mkdtemp(prefix="finalize-fixtures.")
install(FIXTURES, "tmux", TMUX)
install(FIXTURES, "claude", CLAUDE)
NEST_BIN = install(FIXTURES, "nest", NEST)
# argv0 names for the planted claude hop. They are names, not files: `exec -a`
# needs nothing on disk. The first is the name the ancestry walk looks for; the
# second mimics a daemon-hosted session, whose argv0 is the versioned install
# path rather than `claude`.
NEST_AS_CLAUDE = os.path.join(FIXTURES, "as-claude", "claude")
NEST_AS_VERSIONED_CLAUDE = os.path.join(FIXTURES, "claude", "versions", "2.1.270")
FORGE_DIR = os.path.join(FIXTURES, "forge")
os.mkdir(FORGE_DIR)
install(FORGE_DIR, "ps", PS_FORGING)
FORGE_COMMAND_DIR = os.path.join(FIXTURES, "forge-command")
os.mkdir(FORGE_COMMAND_DIR)
install(FORGE_COMMAND_DIR, "ps", PS_FORGE_COMMAND)
MUTE_DIR = os.path.join(FIXTURES, "mute")
os.mkdir(MUTE_DIR)
install(MUTE_DIR, "ps", PS_MUTE_IDENTITY)

# The real ps, except that the `-o command=` read of the planted claude fails.
# $FIXTURE_PS_MODE picks the failure: `status` is a nonzero exit, `empty` is exit 0
# printing nothing (a live pid whose argv the platform withholds). The identity
# read, the ancestry hops, and the whole-table walk use different field lists and
# pass through untouched.
PS_ROOT_COMMAND = r"""#!/bin/bash
set -uo pipefail
fields=""; want=""; prev=""
for arg in "$@"; do
  [ "$prev" = "-o" ] && fields="$arg"
  [ "$prev" = "-p" ] && want="$arg"
  prev="$arg"
done
if [ "$fields" = "command=" ] && [ "$want" = "$(cat "$FIXTURE_ROOT_PID")" ]; then
  [ "${FIXTURE_PS_MODE:-status}" = "empty" ] && exit 0
  echo "ps: no such process" >&2
  exit 1
fi
exec /bin/ps "$@"
"""
PS_ROOT_COMMAND_DIR = os.path.join(FIXTURES, "psrootcommand")
os.mkdir(PS_ROOT_COMMAND_DIR)
install(PS_ROOT_COMMAND_DIR, "ps", PS_ROOT_COMMAND)

# A `date` that removes a directory on its first call, then behaves normally.
# This makes the launcher's own directory disappear mid-run at a fixed point: the
# launcher stamps the handoff filename with `date` before it resolves its own path,
# so the removal lands between those two steps. It models a script whose directory
# is unlinked under a running session.
DATE_RMDIR = r"""#!/bin/bash
set -uo pipefail
if [ -n "${FIXTURE_RM_DIR:-}" ] && [ -e "$FIXTURE_RM_DIR" ]; then
  rm -rf -- "$FIXTURE_RM_DIR"
fi
exec /bin/date "$@"
"""
DATE_RMDIR_DIR = os.path.join(FIXTURES, "datermdir")
os.mkdir(DATE_RMDIR_DIR)
install(DATE_RMDIR_DIR, "date", DATE_RMDIR)
# A mktemp that refuses, so the launcher sees a tmpdir with no room in it. The
# environment cannot produce this portably: macOS `mktemp -t` ignores $TMPDIR, so
# only a refusing mktemp fails on every platform.
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
    finalize-log.*) echo "mktemp: no space left on device" >&2; exit 1 ;;
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
    finalize-goal.*) echo "/nonexistent-finalize-dir/goal"; exit 0 ;;
  esac
done
exec /usr/bin/mktemp "$@"
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


def run(depth=1, panes="%99 ROOTPID 0", tmux_on_path=True, forge_age=None,
        rehost_at=None, tmux_pane=None, sleep=None, mute_identity=False,
        message="handoff", handoff_dir=None, dry_run="1",
        log_mktemp_fails=False, cwd_gone=False, ps_root_command=None,
        self_dir_gone=False, as_claude=None, claude_pid=None,
        forge_command=None, claude_at=None, host_keeps_tmux=False, outer_host=False,
        launcher_flags=()):
    """Launch finalize-session under a real `nest` chain and return what it reported.

    `dry_run` is the raw value of $FINALIZE_DRY_RUN; the empty string means off,
    so the real transport path runs the same code as the dry-run cases.
    """
    workdir = tempfile.mkdtemp(prefix="finalize-case.")
    pidfile = os.path.join(workdir, "root.pid")
    # The tmux-absent case uses NO_TMUX_BIN alone: it must not include REAL_DIRS,
    # since that is where a packaged tmux would be found.
    path = [FIXTURES, REAL_DIRS] if tmux_on_path else [NO_TMUX_BIN]
    if forge_age is not None:
        path.insert(0, FORGE_DIR)
    if mute_identity:
        path.insert(0, MUTE_DIR)
    if forge_command is not None:
        path.insert(0, FORGE_COMMAND_DIR)
    if log_mktemp_fails:
        path.insert(0, NO_LOGFILE_BIN)
    # 'status' or 'empty' - which answer PS_ROOT_COMMAND gives for the planted
    # claude; the fixture above says why a process that has just exited has both.
    if ps_root_command is not None:
        path.insert(0, PS_ROOT_COMMAND_DIR)
    # The launcher is invoked through a symlinked directory that the fixture `date`
    # deletes on its first call. The symlink is what disappears, not the script file,
    # so the launcher keeps running as it would in the field.
    selfdir = None
    if self_dir_gone:
        selfdir = os.path.join(workdir, "selfdir")
        os.mkdir(selfdir)
        os.symlink(LAUNCHER, os.path.join(selfdir, "finalize-session"))
        path.insert(0, DATE_RMDIR_DIR)
    env = {
        "PATH": ":".join(path),
        "HOME": os.environ.get("HOME", workdir),
        "TMPDIR": workdir,
        "FINALIZE_DRY_RUN": dry_run,
        "NEST_PUBLISH_PID": pidfile,
        "FIXTURE_ROOT_PID": pidfile,
        # The claude process the detached transport relaunches as is planted at the
        # top of this chain, so the ancestry walk finds a process this fixture owns
        # rather than whatever real claude happens to be running the suite. Every
        # case gets one, unconditionally: a claude hop the walk cannot reach (the
        # over-the-bound chains) is refused for the same reason a claude hop that
        # was never planted would be, so nothing has to decide which cases need it.
        "NEST_CLAUDE_AT": " ".join(str(d) for d in (
            [depth] if claude_at is None else claude_at if isinstance(claude_at, list) else [claude_at])),
        "NEST_RECORD_CLAUDE": os.path.join(workdir, "claude.pid"),
        "NEST_AS_CLAUDE": as_claude or NEST_AS_CLAUDE,
    }
    # The pid Claude Code hands every tool call as $CLAUDE_PID. Absent by default,
    # so the walk has to find the planted hop by ancestry alone.
    if claude_pid is not None:
        env["CLAUDE_PID"] = str(claude_pid)
    if host_keeps_tmux:
        env["FIXTURE_HOST_KEEPS_TMUX"] = "1"
    if ps_root_command is not None:
        env["FIXTURE_PS_MODE"] = ps_root_command
    if self_dir_gone:
        env["FIXTURE_RM_DIR"] = selfdir
    if forge_age is not None:
        env["FIXTURE_FORGE_AGE"] = forge_age
    if forge_command is not None:
        env["FIXTURE_FORGE_COMMAND"] = forge_command
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
    if sleep is not None:
        env["NEST_SLEEP"] = str(sleep)
    try:
        # The cwd-removing hop sits between the chain and the launcher. The ancestry
        # is unchanged; only the launcher's own cwd is gone.
        launcher = os.path.join(selfdir, "finalize-session") if self_dir_gone else LAUNCHER
        entry = [CWDGONE_BIN, launcher] if cwd_gone else [launcher]
        argv = [NEST_BIN, str(depth)] + entry + list(launcher_flags) + [message]
        # An outer daemon host wraps the whole chain, with its own pane, so the launcher
        # must not mistake another session's host for its own. The trailing `exit` keeps
        # bash from exec-ing the chain in place of the host process.
        if outer_host:
            argv = ["/bin/bash", "-c", 'exec -a "claude --bg-pty-host" /bin/bash -c \'"$@"; exit $?\' host "$@"', "wrap"] + argv
        done = subprocess.run(argv,
                              text=True, capture_output=True, env=env, timeout=120)
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
        if line.startswith("[dry-run] transport=tmux target=target-for-"):
            # The fixture's target is `target-for-<pane>:0.0`; take the pane back
            # out of it rather than matching the whole rendered string.
            return line.split("target-for-", 1)[1].split(" ", 1)[0].split(":", 1)[0]
        if line.startswith("[dry-run] transport=detached "):
            return DETACHED
    if done.returncode == NO_TRANSPORT_RC:
        return DECLINED
    return f"<no decision: rc={done.returncode} out={done.stdout!r} err={done.stderr!r}>"


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



def argv_case(*args, path_prefix=""):
    """Run the launcher with these args against a handoff directory of its own.

    Returns (completed process, list of handoff bodies written). Bodies are returned
    rather than filenames, since the checks concern what a handoff says. The run is
    dry, and `path_prefix` is prepended to the fixture PATH, never substituted for
    it, so a missing transport cannot masquerade as an argv refusal.
    """
    path = ":".join(p for p in (path_prefix, FIXTURES, REAL_DIRS) if p)
    workdir = tempfile.mkdtemp(prefix="finalize-argv.")
    handoffs = os.path.join(workdir, "handoffs")
    try:
        done = subprocess.run(
            [LAUNCHER, *args], text=True, capture_output=True, timeout=30,
            env={"PATH": path, "HOME": workdir, "TMPDIR": workdir,
                 "MEMENTO_HANDOFF_DIR": handoffs, "FINALIZE_DRY_RUN": "1",
                 # The pane's process is this runner, so the launcher descends from it as
                 # it would from a real pane's shell. Without that ancestry the pid reads
                 # as hosted when the suite runs under a host.
                 "TMUX_PANE": "%99", "FIXTURE_PANES": f"%99 {os.getpid()} 0"})
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
# contract's exit code. The goal mktemp runs before the handoff is written, so nothing
# is recorded.
done, bodies = argv_case("a real handoff", path_prefix=NO_TMPDIR_BIN)
check("a tempfile it cannot make is refused as the launcher, not as mktemp",
      done.returncode == REFUSED_RC and "finalize-session:" in done.stderr and bodies == [],
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
shutil.rmtree(logfail_dir, ignore_errors=True)

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

# The flags the successor is relaunched under. A failing `ps` must refuse the handoff
# rather than relaunch the successor without its --model or permission flags. The
# assertion checks the refusal, not only a non-zero status.
nocmd_dir = tempfile.mkdtemp(prefix="finalize-nocmd.")
done = run(panes=None, handoff_dir=nocmd_dir, ps_root_command="status")
recorded = sorted(os.listdir(nocmd_dir))
check("a claude whose command line cannot be read is refused, not relaunched flagless",
      done.returncode == REFUSED_RC and len(recorded) == 1
      and "cannot locate the claude process" in done.stderr
      and os.path.join(nocmd_dir, recorded[0]) in done.stderr,
      f"rc={done.returncode} err={done.stderr!r} recorded={recorded!r}")
shutil.rmtree(nocmd_dir, ignore_errors=True)

# The same fact arriving as success. `ps` exiting 0 with nothing to say is not a
# claude launched with no flags: a pid with no argv is no claude at all, walked
# past like any other. What the walk meets above is the runner's ancestry - a
# real claude on a developer's machine, nothing on CI - so the assertion is that
# the planted pid was not the pick.
done = run(panes=None, ps_root_command="empty")
check("a claude whose command line reads back empty is not read as flagless",
      f"claude_pid={done.root_pid} " not in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

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
# Nothing is recorded, so the refusal must not name a preserved handoff.
rodir = tempfile.mkdtemp(prefix="finalize-ro.")
handoffs_ro = os.path.join(rodir, "handoffs")
os.mkdir(handoffs_ro, 0o500)
try:
    done = subprocess.run(
        [LAUNCHER, "a real handoff"], text=True, capture_output=True, timeout=30,
        env={"PATH": REAL_DIRS, "HOME": rodir, "TMPDIR": rodir,
             "MEMENTO_HANDOFF_DIR": handoffs_ro})
    # Assert the launcher's full sentence: bash's redirection error also contains the
    # launcher's name, so a bare prefix match would not separate the two.
    check("a handoff it cannot write is refused as the launcher, not as the shell",
          done.returncode == REFUSED_RC
          and "finalize-session: cannot write the handoff to" in done.stderr
          and "preserved at" not in done.stderr,
          f"rc={done.returncode} err={done.stderr!r}")
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
# No claude below the host: the only claude is the spawner above it. Relaunching
# that would kill an unrelated session, so the finder stops at the host.
done = run(depth=2, rehost_at=1, panes=PANES_ABOVE_HOST)
check("the session finder never climbs past a host to the spawner's claude",
      picked(done) == DECLINED
      and "detached transport: cannot locate the claude process" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The control: the same chain with no host discovers %99, so the refusals above
# come from the boundary, not from depth or the fixture.
done = run(depth=5, panes=PANES_ABOVE_HOST)
check("the same chain without a host discovers the pane above it",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A host above the pane hosts some other session: this one keeps its pane. This
# is also the suite run from inside a background session.
done = run(depth=5, panes=PANES_ABOVE_HOST, outer_host=True)
check("a host above this session's pane does not make it hosted",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A handoff message naming a host flag is prompt text, not a host.
done = run(depth=5, panes=PANES_ABOVE_HOST, message="next: verify the --bg-pty-host boundary")
check("a handoff message containing --bg-pty-host does not trip the boundary",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# --- no pane to be had, but tmux still gives a transport --------------------
# Refusing a pane is still correct, but it does not mean refusing to deliver: the
# launcher falls back to the detached transport whenever a claude process can be
# found. Every chain here plants one at its top hop, so DECLINED remains only where
# tmux cannot be reached to spawn a fresh window.

done = run(panes=None)
check("no tmux server: falls to the detached transport", picked(done) == DETACHED, picked(done))
# Which claude, exactly. DETACHED alone cannot distinguish the planted process from
# an ambient claude above the test runner, so the pid is named.
check("the detached transport relaunches as the planted claude, not an ambient one",
      f"claude_pid={done.root_pid} " in done.stdout,
      f"root_pid={done.root_pid} out={done.stdout!r} err={done.stderr!r}")
# The launcher captures what the pid is holding and hands it to the worker to re-check
# before any kill. The captured text must describe the process actually found; a
# constant or the bare pid would pass a non-empty check and prove nothing.
check("the launcher captures the old process's identity, not just its pid",
      any(line.startswith("[dry-run] old-process identity: ") and NEST_AS_CLAUDE in line
          for line in done.stdout.splitlines()),
      f"out={done.stdout!r}")


def identity_names(done, name):
    return any(line.startswith("[dry-run] old-process identity: ") and name in line
               for line in done.stdout.splitlines())


# The shape of a daemon-hosted session: the process is `.../claude/versions/<ver>
# --session-id ...`, its parent is the same binary with `--bg-pty-host`, and above
# that is pid 1. Matching by basename climbs past both and reaches nothing; the
# naming assertion stops a walk that climbs into an ambient claude from passing.
done = run(panes=None, as_claude=NEST_AS_VERSIONED_CLAUDE)
check("a claude running as the versioned binary path is found, not walked past",
      picked(done) == DETACHED and f"claude_pid={done.root_pid} " in done.stdout
      and identity_names(done, NEST_AS_VERSIONED_CLAUDE),
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The versioned path is matched only as argv0. The daemon's `--bg-pty-host` process
# and a shell can both contain that path as an argument, and matching the whole
# command line would accept them as the claude and terminate the host along with
# every session under it. This process has a plain name with the versioned path in
# its argv, so the launcher must walk past it. The pick is whatever the ambient
# ancestry yields, so the assertion is only that the planted pid was not picked.
done = run(panes=None, as_claude=os.path.join(FIXTURES, "not-claude", "zsh"),
           message=f"ls {NEST_AS_VERSIONED_CLAUDE}; handoff")
check("a versioned claude path in the arguments, not argv0, is not a claude",
      f"claude_pid={done.root_pid} " not in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# $CLAUDE_PID names the session's own pid for every tool call, and the named pid wins
# over ancestry. The chain still plants a claude at its top hop, and the launcher must
# relaunch the named one instead. The named process is not in this launcher's ancestry,
# so only the environment can explain how it was found. It runs in its own session so
# the whole group can be terminated, since nest runs its command as a child.
NAMED_CLAUDE = subprocess.Popen(
    [NEST_BIN, "0", "sleep", "600"],
    env={**os.environ, "NEST_CLAUDE_AT": "0", "NEST_AS_CLAUDE": NEST_AS_CLAUDE},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
# The rename happens during nest's startup, after Popen returns. Without waiting for
# it, the launcher would read argv0 as /bin/bash and refuse for a timing reason.
for _ in range(100):
    if subprocess.run(["ps", "-o", "command=", "-p", str(NAMED_CLAUDE.pid)],
                      capture_output=True, text=True).stdout.startswith(NEST_AS_CLAUDE + " "):
        break
    time.sleep(0.05)
try:
    done = run(panes=None, claude_pid=NAMED_CLAUDE.pid)
    check("$CLAUDE_PID names the claude to relaunch ahead of the ancestry walk",
          picked(done) == DETACHED and f"claude_pid={NAMED_CLAUDE.pid} " in done.stdout
          and identity_names(done, NEST_AS_CLAUDE),
          f"named={NAMED_CLAUDE.pid} root_pid={done.root_pid} rc={done.returncode} "
          f"out={done.stdout!r} err={done.stderr!r}")
finally:
    os.killpg(NAMED_CLAUDE.pid, 15)
    NAMED_CLAUDE.wait()

# A named pid that is not a claude is refused, not used as a starting point for the
# walk. STRANGER's ancestry is this runner's ancestry, which may end in a real claude
# session that the walk would find and terminate. The planted claude is unreachable
# here too: the named pid is the whole answer.
done = run(panes=None, claude_pid=STRANGER.pid)
check("$CLAUDE_PID naming a non-claude process is refused, not walked past",
      done.returncode == NO_TRANSPORT_RC
      and "cannot locate the claude process" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A `--print` claude is a headless SDK bridge with no input box, so a fresh tmux
# window cannot stand in for it. Relaunching it interactively would sever the remote
# client driving it. The command line is forged onto the planted hop.
done = run(panes=None, forge_command=(
    "/x/claude/versions/2.1.283 --print --sdk-url https://api.anthropic.com/v1/code/sessions/cse_1"
    " --session-id cse_1 --input-format stream-json --model claude-opus-5-5"))
check("a headless (--print) claude is not a session to relaunch",
      f"claude_pid={done.root_pid} " not in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The daemon's pty host runs the same binary, and the ancestry walk meets it after
# declining a session. Terminating the host would take every session it hosts, so it
# is declined by its flags too. Both walks continue into the runner's ancestry, so the
# assertion is only that the planted pid was not picked.
done = run(panes=None, forge_command=(
    "/x/claude --bg-pty-host /tmp/cc-daemon/pty/a.sock 148 38 --"
    " /x/claude/versions/2.1.283 --session-id a --permission-mode bypassPermissions"))
check("the daemon's --bg-pty-host process is not a session to relaunch",
      f"claude_pid={done.root_pid} " not in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The headless check reads only the flag region. A session started from a prior
# handoff carries that handoff as its positional prompt, unquoted in ps output, and
# the prompt can contain `--print` in prose.
done = run(panes=None, forge_command=(
    "/x/claude --permission-mode plan the ceiling hook ignores --print sessions; run /next"))
check("a prompt that mentions --print does not make the session headless",
      picked(done) == DETACHED and "flags=[--permission-mode plan]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The relaunching transports carry a requested model and effort as claude flags, after the
# old session's own flags so the request wins.
done = run(panes=None, forge_command="/x/claude --model sonnet --permission-mode plan",
           launcher_flags=("--model", "opus[1m]", "--effort", "high"))
check("a detached relaunch starts on the requested model and effort",
      picked(done) == DETACHED
      and "flags=[--model sonnet --permission-mode plan --model opus\\[1m\\] --effort high]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
done = run(panes=None, forge_command="/x/claude --permission-mode plan")
check("and without a request the relaunch flags are only the old session's",
      "flags=[--permission-mode plan]" in done.stdout and "--effort" not in done.stdout,
      f"rc={done.returncode} out={done.stdout!r}")

# The relaunch line is parsed by a shell (tmux's default-shell, or the terminal's). An
# unquoted `--model opus[1m]` is a glob there, which zsh aborts on when nothing matches,
# and a quote in the handoff's path unbalances the line. The dry run prints the line the
# worker runs; it is checked through zsh and through bash's failglob and must yield the
# exact argv.
quoted_dir = tempfile.mkdtemp(prefix="o'neil [1m] ")
done = run(panes=None, handoff_dir=quoted_dir, forge_command=(
    "/x/claude --model opus[1m] --permission-mode bypassPermissions run /next"))
line = done.stdout.partition("would launch: ")[2].partition(" in a fresh detached")[0]
shells = [["bash", "-O", "failglob", "-c"]] + ([["zsh", "-f", "-c"]] if shutil.which("zsh") else [])
for shell in shells:
    parsed = subprocess.run(shell + ['cat() { printf "%s\\n" "$@"; }; printf "%s\\n" ' + line],
                            capture_output=True, text=True)
    argv = parsed.stdout.split("\n")
    check(f"the relaunch line reaches {shell[0]} as the exact argv, glob-shaped model and quoted path included",
          picked(done) == DETACHED and parsed.returncode == 0
          and argv[1:5] == ["--model", "opus[1m]", "--permission-mode", "bypassPermissions"]
          and argv[5].startswith(quoted_dir + "/"),
          f"line={line!r} parsed={parsed!r} out={done.stdout!r}")
shutil.rmtree(quoted_dir)

# The flags a daemon-hosted session is launched with, in the order the daemon writes
# them: harness flags first, `--permission-mode` last. Each flag before it must be
# consumed with its value, or the value is read as the prompt and the scan halts before
# the permission mode.
DAEMON_ARGV = ("/x/claude/versions/2.1.283 --session-id a00eaf96 --fork-session"
               " --resume /x/p.jsonl --allowed-tools mcp__a --allowed-tools mcp__b"
               " --permission-mode bypassPermissions")
done = run(panes=None, forge_command=DAEMON_ARGV)
check("a daemon-hosted session's harness flags are consumed and its permission mode kept",
      picked(done) == DETACHED and "flags=[--permission-mode bypassPermissions]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# Each flag claude takes is dropped with the number of values its arity specifies, so
# the flag that matters is still reached. The prompt here is a recap dense with dash
# tokens, as a handoff flattened onto one line by ps would be, and none of them may be
# read as a flag.
done = run(panes=None, forge_command=(
    "/x/claude -c --add-dir /a /b --allowed-tools Read Edit --debug api -w --effort high"
    " --permission-mode plan === recap === - shipped --print support - fixed"
    " --permission-mode handling - never --dangerously-skip-permissions"))
check("variadic and boolean flags are dropped by arity and the scan halts at the prompt",
      picked(done) == DETACHED and "flags=[--permission-mode plan]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A flag the table does not know: whether the next token is its value or the prompt
# cannot be determined. The launcher refuses, names the flag, and leaves the handoff on
# disk rather than guessing.
done = run(panes=None, forge_command="/x/claude --frobnicate maybe --permission-mode plan")
check("an unknown claude flag is a refusal naming the flag, not a guess at the prompt",
      done.returncode == NO_TRANSPORT_RC and "cannot read the flags" in done.stderr
      and "--frobnicate" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The daemon itself: a subcommand where a session's prompt would be. The walk passes it
# like the pty host it spawns, so the assertion has the same shape.
done = run(panes=None, forge_command="/x/claude daemon run")
check("a claude subcommand (daemon run) is not a session to relaunch",
      f"claude_pid={done.root_pid} " not in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A pre-warmed spare, as ps shows one live: its subcommand spelling must not be read
# as the prompt.
done = run(panes=None, forge_command="/x/claude bg-spare --bg-spare /tmp/cc/spare/b3.claim.sock")
check("a daemon spare (bg-spare --bg-spare) is not a session to relaunch",
      f"claude_pid={done.root_pid} " not in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A host subcommand counts only with its own follow-up token; a prompt that merely
# starts with the word is a session's prompt.
done = run(panes=None, forge_command="/x/claude --permission-mode plan bg-pty-host leaks are fixed")
check("a prompt starting with a host word is still a session",
      picked(done) == DETACHED and f"claude_pid={done.root_pid} " in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# $CLAUDE_PID holding something that is not a pid at all is a refusal in the
# launcher's own voice, not the shell's `integer expression expected` diagnostic.
done = run(panes=None, claude_pid="abc")
check("a non-numeric $CLAUDE_PID is refused cleanly",
      done.returncode == NO_TRANSPORT_RC and "cannot locate the claude process" in done.stderr
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
check("tmux absent from PATH: this is the one true decline - nothing left to spawn a window with",
      picked(done) == DECLINED, picked(done))
# The exit code is the no-transport report a caller acts on. It is pinned once here,
# against the sole remaining decline case, rather than beside every refused-pane case below.
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

# Liveness is checked on two paths: the walk screens what enters discovery, and
# `_pane_target` screens the inherited $TMUX_PANE. Every dead-pane case above is decided
# by the second, so this case pins the walk's own check, which mutation testing showed
# the suite would otherwise miss.
#
# Order matters because the walk keeps one pane per pid, last row winning: listed
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

done = run(depth=2, sleep=2, forge_age="00:00")
check("an ancestor younger than its own descendant is refused as a recycled pid - falls to detached",
      picked(done) == DETACHED, picked(done))
# The control runs under the forging ps, differing only in the value written. Without
# it, a mangled process table could decline cleanly with the no-transport code for a
# reason unrelated to the age check. Only a run that forges and still resolves shows
# the wrapper leaves the subject intact.
check("the same forged pid resolves when the age is older instead of younger",
      picked(run(depth=2, sleep=2, forge_age="99:00:00")) == "%99",
      "without this the forging fixture, not the age check, could be doing the work")

# The walk refuses an elapsed time it cannot parse rather than treating it as age zero.
# Pinning that takes a value the format rejects but ageof() still reads as large. `abc`
# folds to zero, which the age check already refuses, so the format guard would go
# untested. `1:2:3:4` has too many fields for [[dd-]hh:]mm:ss yet folds to about 62
# hours, so only the format check can refuse it.
done = run(depth=2, sleep=2, forge_age="1:2:3:4")
check("an elapsed time the walk cannot read refuses the pane - falls to detached",
      picked(done) == DETACHED, picked(done))

# --- precedence -----------------------------------------------------------
# Both candidates must be resolvable, or the case cannot tell which one won. Here
# discovery can reach %99 and the environment names %42, so the two genuinely compete.

done = run(depth=2, panes=f"%99 ROOTPID 0\n%42 {STRANGER.pid} 0", tmux_pane="%42")
check("an inherited $TMUX_PANE wins outright and discovery never runs",
      picked(done) == "%42", f"rc={done.returncode} out={done.stdout!r}")

# A $TMUX_PANE naming a pane that has since gone must not consume the attempt. tmux
# answers display-message for an unowned pane with exit 0 and empty fields, so the
# target renders as the bare ":." (a live address for the current pane). Taking it
# would hand the session to whichever window is focused.
done = run(depth=2, tmux_pane="%4242")
check("a stale $TMUX_PANE hands the question on and discovery answers it",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r}")

# With nothing to fall back to, the answer is still no pane, never the ':.' an
# unresolvable id renders into. The launcher still has the detached transport to try.
done = run(depth=2, panes="%5 1 0", tmux_pane="%4242")
check("a stale $TMUX_PANE with nothing to discover falls to detached rather than guessing a pane",
      picked(done) == DETACHED, picked(done))

# Owning a pane and displaying a live process are different facts; remain-on-exit keeps
# tmux answering for a pane whose process has exited. An inherited $TMUX_PANE can name
# such a dead pane, which echoes its id just as a live one does, so an id-only check
# would send the handoff into it.
done = run(depth=2, panes=f"%99 ROOTPID 0\n%77 {STRANGER.pid} 1", tmux_pane="%77")
check("an inherited $TMUX_PANE naming a dead pane loses to a live discovered one",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r}")

# ':.' is what an unresolvable pane id renders into, and tmux resolves it to the
# current pane, which is live. Liveness cannot refuse it, so only comparing the echoed
# id against the id requested can. %77 is listed first so 'current' is a pane the
# ancestry does not own, which makes taking it a wrong answer rather than a lucky one.
done = run(depth=2, panes=f"%77 {STRANGER.pid} 0\n%99 ROOTPID 0", tmux_pane=":.")
check("a $TMUX_PANE that resolves to someone else's live pane is refused",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r}")

# --- the detached worker: what it retires, and in what order ----------------
# The launcher cases above run in dry-run, so the detached transport's promise (after
# the handoff, exactly one live agent) is not covered there. These cases run the real
# worker against the fixture tmux and a real victim process, and each is decided by
# whether a process is alive or dead when it finishes.


# A victim that ignores SIGTERM, so retirement has to escalate to KILL. The default
# `sleep 600` dies on the first signal and never reaches the KILL branch.
#
# The victim's `ps` command string must stay stable from birth, so it is a shell rather
# than a python process: the interpreter rewrites its own command string during startup,
# and retirement correctly refuses to signal a process whose identity changed. The
# trailing `sleep 1` loop keeps the shell blocked without a long-lived child, so when KILL
# lands on the shell, the orphaned sleep exits within a second and the case leaves
# nothing behind.
IGNORES_TERM = ["/bin/bash", "-c", 'trap "" TERM; while :; do sleep 1; done']


def worker_case(identity, goal="", fail_on=None, pane_text=None, victim=("sleep", "600")):
    """Run one detached worker over a real process standing in for the old claude.

    Returns (retired, done): whether the victim was signalled, and the worker's
    result, where `done.tmux_log` lists every tmux argv the worker issued. The
    identity is read with the same `ps` fields the launcher captures; identity="match"
    passes that reading, and identity="reused" swaps the command out, as happens when
    the OS reissues the pid. `pane_text` sets what the new session's pane shows.
    """
    proc = subprocess.Popen(list(victim),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    read = subprocess.run(["ps", "-o", "lstart=,command=", "-p", str(proc.pid)],
                          text=True, capture_output=True)
    # `$(...)` strips trailing newlines and nothing else; matching that exactly
    # matters, because the whole check is a string comparison against it.
    real = read.stdout.rstrip("\n")
    if identity == "match":
        ident = real
    else:
        ident = real.replace(" ".join(victim), "vi Makefile")
        # A substitution that matches nothing would leave the real identity under the name
        # "reused", and the case would pass while testing the opposite.
        assert ident != real, f"reused-identity fixture altered nothing: {real!r}"
    workdir = tempfile.mkdtemp(prefix="finalize-worker.")
    msgfile = os.path.join(workdir, "msg")
    goalfile = os.path.join(workdir, "goal")
    with open(msgfile, "w") as handle:
        handle.write("handoff")
    with open(goalfile, "w") as handle:
        handle.write(goal)
    tmuxlog = os.path.join(workdir, "tmux.log")
    env = {
        "PATH": ":".join([FIXTURES, REAL_DIRS]),
        "HOME": os.environ.get("HOME", workdir),
        "TMPDIR": workdir,
        "FIXTURE_TMUX_LOG": tmuxlog,
    }
    if fail_on is not None:
        env["FIXTURE_TMUX_FAIL"] = fail_on
    if pane_text is not None:
        env["FIXTURE_TMUX_PANE_TEXT"] = pane_text
    try:
        done = subprocess.run(
            [LAUNCHER, "--detached-worker", "fixture-session", workdir, msgfile,
             goalfile, "", str(proc.pid), "/bin/true", ident],
            text=True, capture_output=True, env=env, timeout=180)
        try:
            proc.wait(timeout=5)
            retired = True
        except subprocess.TimeoutExpired:
            retired = False
        done.tmux_log = open(tmuxlog).read() if os.path.exists(tmuxlog) else ""
        return retired, done
    finally:
        proc.kill()
        proc.wait()
        shutil.rmtree(workdir, ignore_errors=True)


# Each worker sleeps out the full handoff delay, so the cases run concurrently. Each
# owns its victim process and workdir.
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    same_pid = pool.submit(worker_case, "match")
    reused_pid = pool.submit(worker_case, "reused")
    # The goal delivery is staged to fail at its first tmux call, which under the
    # worker's `set -euo pipefail` aborts the script on the spot.
    goal_fails = pool.submit(worker_case, "match", goal="ship the thing",
                             fail_on="load-buffer")
    # The spawn that never happens and the spawn that never comes up are the worker's
    # two "successor is not there" arms. Both leave the old process running.
    spawn_fails = pool.submit(worker_case, "match", fail_on="new-session")
    # A pane that shows something other than the boot banner for the whole readiness
    # window, as in a slow or wedged boot. It waits out the full ready timeout on top of
    # the handoff delay, so it is the longest case.
    never_ready = pool.submit(worker_case, "match", goal="ship the thing",
                              pane_text="waiting for the shell to start")
    # A victim that ignores TERM, which is the only way to reach the KILL escalation.
    # Every other case here dies on the first signal.
    survives_term = pool.submit(worker_case, "match", victim=IGNORES_TERM)

retired, done = same_pid.result()
check("the worker retires the old process it was given",
      retired, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The window between capturing the pid and signalling it spans the handoff delay and
# the readiness wait, which is ample time for the OS to reissue the pid. `kill -0`
# cannot detect that, since it reports only that something is alive.
retired, done = reused_pid.result()
check("a pid reissued to another process during the wait is not signalled",
      not retired, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
check("and the worker says why it left that pid alone",
      "not the process captured at launch" in done.stdout, f"out={done.stdout!r}")

# Ordering, not a guard, protects the single-live-agent guarantee: the goal delivery
# below retirement is optional and fallible, and `set -e` aborts on its failure.
# Retirement runs first, so the abort has nothing left to suppress.
retired, done = goal_fails.result()
check("a failed goal delivery cannot leave the old process alive",
      retired, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A successor that cannot be spawned at all. The old process is the only agent, so it
# must still run when the worker gives up, and the worker must report the failure
# rather than exit 0.
retired, done = spawn_fails.result()
check("a session that cannot be created leaves the old process running",
      not retired, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
check("and the failed spawn is reported as a failure, not a quiet no-op",
      done.returncode == 1 and "could not create detached tmux session" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# Nothing was created, so nothing is torn down. This keeps the teardown assertion below
# honest: a worker that killed the session on every failure path would pass that one
# while being wrong here.
check("a spawn that never happened tears nothing down",
      "kill-session" not in done.tmux_log, f"tmux_log={done.tmux_log!r}")

# A readiness timeout does not mean the successor got nothing: the handoff rides in the
# `tmux new-session` argv, and the successor is usually still booting. If that boot
# finishes after the deadline, two live agents would exist, so the worker tears the new
# session down. Only the tmux log shows this, since the worker's own log line says the
# same either way.
retired, done = never_ready.result()
check("a successor that never becomes ready is torn down, not left racing the old process",
      "kill-session -t fixture-session" in done.tmux_log,
      f"rc={done.returncode} out={done.stdout!r} tmux_log={done.tmux_log!r}")
check("and the old process survives the timeout - a spawn that never came up kills nobody",
      not retired, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
check("and the timeout is reported as a failure, with the undelivered goal named",
      done.returncode == 1 and "did not become ready" in done.stdout
      and "carried goal NOT delivered" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# Retirement escalates when TERM is refused. Every other victim dies on the first
# signal, so without this case the KILL branch would never run.
retired, done = survives_term.result()
check("a process that refuses TERM is still retired, by escalation to KILL",
      retired, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
check("and the escalation says so, rather than the pid quietly disappearing",
      "survived TERM; sending KILL" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r}")


# ---------------------------------------------------------------------------
# The handoff is durable, and every delivery names it.
#
# A handoff that exists only in flight is lost on the first failed paste. These cases
# check that the payload is on disk before any transport is chosen, that no code path
# removes it, and that the message carries its own path.
# ---------------------------------------------------------------------------

def tmux_worker_case(pane_text, timeout="2", settings=()):
    """Run one tmux worker and report what it did with the handoff.

    Returns (done, whether the handoff file still exists, tmux log). `pane_text` is
    what capture-pane shows, so a reset that never registers is a value, not a mode.
    """
    workdir = tempfile.mkdtemp(prefix="finalize-tmux.")
    msgfile = os.path.join(workdir, "handoff.md")
    goalfile = os.path.join(workdir, "goal")
    with open(msgfile, "w") as handle:
        handle.write("the whole handoff")
    open(goalfile, "w").close()
    tmuxlog = os.path.join(workdir, "tmux.log")
    env = {
        "PATH": ":".join([FIXTURES, REAL_DIRS]),
        "HOME": os.environ.get("HOME", workdir),
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


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    registered = pool.submit(tmux_worker_case, "Claude Code v1.2.3")
    never = pool.submit(tmux_worker_case, "still working on the previous turn")

done, survived, log = registered.result()
check("a reset the worker can see delivers the handoff",
      "paste-buffer" in log, f"out={done.stdout!r} log={log!r}")
check("and the handoff file outlives the worker that delivered it",
      survived, f"out={done.stdout!r}")

# The reset can be typed while the agent is still mid-turn, so Claude queues it and the
# banner may not appear within any fixed window. Tmux type-ahead preserves ordering, so
# the worker sends regardless and never deletes a handoff it could not confirm.
done, survived, log = never.result()
check("a reset the worker never sees still delivers the handoff",
      "paste-buffer" in log, f"out={done.stdout!r} log={log!r}")
check("and says plainly that it could not confirm the reset",
      "not observed" in done.stdout, f"out={done.stdout!r}")
check("and never claims the handoff was withheld",
      "NOT sent" not in done.stdout and "misfired" not in log,
      f"out={done.stdout!r} log={log!r}")
check("and above all does not delete the handoff it could not confirm",
      survived, f"out={done.stdout!r}")

# The requested model and effort are typed into the reset session before the handoff is
# pasted, so the handoff's first turn already runs on them.
done, _, log = tmux_worker_case("Claude Code v1.2.3", settings=("/model opus", "/effort high"))
model_at, effort_at, paste_at = (log.find(needle) for needle in
                                 ("-l /model opus", "-l /effort high", "paste-buffer"))
check("the worker types the requested /model and /effort, in order, before the handoff",
      0 <= model_at < effort_at < paste_at, f"out={done.stdout!r} log={log!r}")
done, _, log = tmux_worker_case("Claude Code v1.2.3")
check("with nothing requested the worker types no setting",
      " -l " not in log and "paste-buffer" in log, f"out={done.stdout!r} log={log!r}")

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

shutil.rmtree(FIXTURES, ignore_errors=True)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
