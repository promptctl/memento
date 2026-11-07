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
    # What Claude Code hands every tool call: the pid of the session's own claude.
    [ -n "${NEST_EXPORT_CLAUDE_PID:-}" ] && export CLAUDE_PID=$$
    exec -a "$NEST_AS_CLAUDE" /bin/bash "$self" "$depth" "$@" ;;
esac
if [ "$depth" -gt 0 ]; then
  if [ "${NEST_REHOST_AT:-}" = "$depth" ]; then
    unset NEST_REHOST_AT
    # The host's own argv, carried in argv0 (see `carried`), so the launcher reads
    # this hop as a real `claude --bg-pty-host`; a shebang script alone is argv0 /bin/bash.
    sep=$'\x1f'
    (exec -a "${NEST_HOST_AS:-${sep}claude${sep}--bg-pty-host}" /bin/bash "$(command -v claude)" daemon run --bg-pty-host -- "$self" $((depth - 1)) "$@")
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

PS = r"""#!/bin/bash
# The real ps, with the forgeries a case asks for written into the tree the
# launcher's walk reads (`ps -eo pid=,ppid=,etime=`; the argv comes from perl).
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
#   $FIXTURE_MUTE_IDENTITY  the identity read (`-o lstart=,command= -p <pid>`)
#                           exits 0 printing nothing, which is what a pid whose
#                           holder exited reports: success with no identity in it.
# Every other ps call passes straight through.
set -uo pipefail
for arg in "$@"; do
  [ "$arg" = "lstart=,command=" ] && [ -n "${FIXTURE_MUTE_IDENTITY:-}" ] && exit 0
done
[ "${1:-}" = "-eo" ] || exec /bin/ps "$@"
[ -z "${FIXTURE_PS_FAILS:-}" ] || { echo "ps: fixture refuses the process table" >&2; exit 1; }
/bin/ps "$@" | /usr/bin/awk -v root="$(cat "$FIXTURE_ROOT_PID")" -v runner="${FIXTURE_RUNNER_PID:-}" \
    -v age="${FIXTURE_FORGE_AGE:-}" '
  {
    ppid = $1 == runner ? 1 : $2
    etime = ($1 == root && age != "") ? age : $3
    print $1, ppid, etime
  }'
"""

# A hop's argv, given as a list. `nest` owns every token after a hop's argv0, so a
# hop carries the argv a case wants in its argv0 instead: the unit separator, then
# the elements joined by it. `carried` builds that argv0, and the perl fixture below
# reads it back as the row's whole argv.
SEP = "\x1f"


def carried(argv):
    return SEP + SEP.join(argv)


PERL = r"""#!/bin/bash
# The real perl, so the launcher's process table is the kernel's argv read, with
# one forgery: a row whose argv0 begins with the unit separator (see `carried`)
# takes the elements carried there as its whole argv. The real perl has already
# encoded them, separator included as the text \x1f, so they split on that text
# with no decoding, and an empty element is written as \c, the encoding of one.
set -uo pipefail
/usr/bin/perl "$@" | /usr/bin/awk '
  $4 !~ /^\\x1f/ { print; next }
  {
    row = $1 " " $2 " " $3
    rest = substr($4, 5)
    if (rest != "") {
      n = split(rest, parts, /\\x1f/)
      for (i = 1; i <= n; i++) row = row " " (parts[i] == "" ? "\\c" : parts[i])
    }
    print row
  }'
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
PS_DIR = os.path.join(FIXTURES, "ps")
os.mkdir(PS_DIR)
install(PS_DIR, "ps", PS)
install(PS_DIR, "perl", PERL)

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
        log_mktemp_fails=False, cwd_gone=False, ps_fails=False,
        self_dir_gone=False, as_claude=None, claude_pid=None,
        claude_at=None, host_keeps_tmux=False, outer_host=False,
        launcher_flags=(), host_as=None, export_claude_pid=False):
    """Launch finalize-session under a real `nest` chain and return what it reported.

    `dry_run` is the raw value of $FINALIZE_DRY_RUN; the empty string means off,
    so the real transport path runs the same code as the dry-run cases.
    `as_claude` is the planted claude's argv0 (a string), or its whole argv (a
    list); `host_as` is the host hop's whole argv.
    """
    workdir = tempfile.mkdtemp(prefix="finalize-case.")
    pidfile = os.path.join(workdir, "root.pid")
    # The tmux-absent case uses NO_TMUX_BIN in place of FIXTURES and REAL_DIRS: it must
    # not include REAL_DIRS, since that is where a packaged tmux would be found.
    # The ps fixture sits first on every case's PATH; a case changes only the
    # forgeries it writes.
    path = [PS_DIR] + ([FIXTURES, REAL_DIRS] if tmux_on_path else [NO_TMUX_BIN])
    if log_mktemp_fails:
        path.insert(0, NO_LOGFILE_BIN)
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
        "FIXTURE_RUNNER_PID": str(os.getpid()),
        # The claude process the detached transport relaunches as is planted at the
        # top of this chain, so the ancestry walk finds a process this fixture owns
        # rather than whatever real claude happens to be running the suite. Every
        # case gets one, unconditionally: a claude hop the walk cannot reach (the
        # over-the-bound chains) is refused for the same reason a claude hop that
        # was never planted would be, so nothing has to decide which cases need it.
        "NEST_CLAUDE_AT": " ".join(str(d) for d in (
            [depth] if claude_at is None else claude_at if isinstance(claude_at, list) else [claude_at])),
        "NEST_RECORD_CLAUDE": os.path.join(workdir, "claude.pid"),
        "NEST_AS_CLAUDE": (carried(as_claude) if isinstance(as_claude, list)
                           else as_claude or NEST_AS_CLAUDE),
    }
    # The pid Claude Code hands every tool call as $CLAUDE_PID. Absent by default,
    # so the walk has to find the planted hop by ancestry alone.
    if claude_pid is not None:
        env["CLAUDE_PID"] = str(claude_pid)
    # Set from inside the chain: the planted claude's pid exists only once it runs.
    if export_claude_pid:
        env["NEST_EXPORT_CLAUDE_PID"] = "1"
    if host_as is not None:
        env["NEST_HOST_AS"] = carried(host_as)
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
            argv = ["/bin/bash", "-c", 'exec -a "$0" /bin/bash -c \'"$@"; exit $?\' host "$@"',
                    carried(["claude", "--bg-pty-host"])] + argv
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
    # The pane's process is this runner, and the launcher runs under a claude below
    # it, as it would in a real pane's session. The trailing `exit` keeps bash from
    # exec-ing the launcher in place of the claude process.
    rootfile = os.path.join(workdir, "root.pid")
    with open(rootfile, "w") as handle:
        handle.write(str(os.getpid()))
    under_claude = ["/bin/bash", "-c", 'exec -a "$0" /bin/bash -c \'"$@"; exit $?\' wrap "$@"',
                    NEST_AS_CLAUDE]
    try:
        done = subprocess.run(
            [*under_claude, LAUNCHER, *args], text=True, capture_output=True, timeout=30,
            env={"PATH": path, "HOME": workdir, "TMPDIR": workdir,
                 "MEMENTO_HANDOFF_DIR": handoffs, "FINALIZE_DRY_RUN": "1",
                 "FIXTURE_ROOT_PID": rootfile, "FIXTURE_PANES": "%99 ROOTPID 0"})
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

# A row with no argv, as for a pid whose argv the kernel withholds, is not a claude
# launched with no flags: it is no claude at all, walked past like any other. The walk then
# leaves the chain with no claude, so nothing is handed off.
done = run(panes=None, as_claude=[])
check("a claude whose argv reads back empty is not read as flagless",
      done.returncode == NO_TRANSPORT_RC and "the ancestry walk passed no claude" in done.stderr
      and f"claude_pid={done.root_pid} " not in done.stdout,
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

# The boundary is read from the shape of the tree: a claude other than this session's
# own, met before a live pane, means the pane shows that claude's session. Each shape
# runs both with $CLAUDE_PID, as Claude Code sets it for every tool call, and without
# it, where the first claude met stands in.
UNREADABLE_HOST = "claude --frobnicate value --bg-pty-host /tmp/cc-daemon/pty/a.sock".split()
for export in (False, True):
    how = "with $CLAUDE_PID" if export else "without $CLAUDE_PID"
    # A host whose flags _claude_flags cannot read (an unlisted flag before a value)
    # is still a claude between this session and the spawner's pane.
    done = run(depth=5, rehost_at=3, claude_at=[5, 2], panes=PANES_ABOVE_HOST,
               host_as=UNREADABLE_HOST, export_claude_pid=export)
    check(f"a host whose flags cannot be read still keeps the pane above it out of reach ({how})",
          picked(done) == DETACHED, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
    # A claude started inside another claude's shell, below the outer one's pane.
    done = run(depth=5, claude_at=[4, 2], panes=PANES_ABOVE_HOST, export_claude_pid=export)
    check(f"a claude inside another claude never takes the outer one's pane ({how})",
          picked(done) == DETACHED, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
    # The same, where the pane's own process is the outer claude.
    done = run(depth=5, claude_at=[5, 2], panes=PANES_ABOVE_HOST, export_claude_pid=export)
    check(f"a pane whose process is another claude is never this session's ({how})",
          picked(done) == DETACHED, f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
    # This session's own claude, one hop below its pane, never counts against it,
    # whatever its flags: an unlisted flag before a value, and a prompt naming the
    # host shapes after it, still leave the pane its own.
    for argv in ("--frobnicate value", "--frobnicate value the daemon run hung on --bg-pty-host"):
        done = run(depth=2, claude_at=[1], export_claude_pid=export,
                   as_claude=[NEST_AS_CLAUDE, *argv.split()])
        check(f"this session's own claude reading `{argv}` keeps its pane ({how})",
              picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# $CLAUDE_PID names this session's claude even when its flags read as not a session,
# so the walk never counts it as another claude.
done = run(depth=2, claude_at=[1], export_claude_pid=True, as_claude=[NEST_AS_CLAUDE, "--print"])
check("the claude $CLAUDE_PID names is never another claude, even reading as --print",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A $CLAUDE_PID beyond the 16-hop bound is unproven, so relaunching it could signal a
# session that never asked: it is refused, not relaunched.
done = run(depth=17, claude_at=[16], panes=None, export_claude_pid=True)
check("a $CLAUDE_PID beyond the walk's reach is refused, not relaunched",
      done.returncode == NO_TRANSPORT_RC and "the ancestry walk never passed $CLAUDE_PID" in done.stderr
      and "16-hop bound" in done.stderr and f"claude_pid={done.session_claude_pid} " not in done.stdout,
      f"ancestry={ancestry(done)!r} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# The own claude is identified by the walk passing it, not by argv0: a claude run under
# node keeps its pane. A relaunch rebuilds it from its command line, so with no pane
# that command line must be a claude binary's.
NODE_CLAUDE = ["/usr/local/bin/node", "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"]
done = run(depth=2, claude_at=[1], export_claude_pid=True, as_claude=NODE_CLAUDE)
check("a $CLAUDE_PID claude run under node keeps its pane",
      picked(done) == "%99", f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
done = run(depth=2, claude_at=[1], export_claude_pid=True, as_claude=NODE_CLAUDE, panes=None)
check("a $CLAUDE_PID claude run under node is not relaunched",
      done.returncode == NO_TRANSPORT_RC and "does not run as a claude binary" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# Without $CLAUDE_PID, a pane reached before any claude shows no session of this one's,
# so nothing is typed into it.
done = run(depth=2, claude_at=[])
check("a pane reached with no claude below it is refused, not cleared",
      done.returncode == NO_TRANSPORT_RC and "the ancestry walk passed no claude" in done.stderr
      and picked(done) != "%99",
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A tmux server the walk climbs through lists no live pane of this session (another
# socket, or a tmux that cannot report it), so whatever terminal shows this session is
# not its own to type into: confined, never paneless.
done = run(depth=2, claude_at=[1], panes=None, rehost_at=2, host_as=["tmux", "new-session", "-s", "other"])
check("a tmux server above this session with no pane of it confines the session",
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
# The relaunch target is the same own claude the hosted walk reads. Without $CLAUDE_PID,
# a headless claude started inside an interactive session's shell is this process's own
# claude, and the interactive one above it is another session, never relaunched in its place.
done = run(depth=5, claude_at=[2], as_claude=[NEST_AS_CLAUDE, "--print"], panes=None,
           rehost_at=3, host_as=[NEST_AS_CLAUDE, "--permission-mode", "plan"])
check("a headless own claude is refused, not exchanged for the interactive claude above it",
      done.returncode == NO_TRANSPORT_RC and "cannot locate the claude process" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

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

# $CLAUDE_PID names the session's own pid for every tool call, and it is this session's
# only if the walk passes it. This named claude is a live claude outside the launcher's
# ancestry. It runs in its own session so the whole group can be terminated, since
# nest runs its command as a child.
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

# A named pid that is not a claude is refused, never replaced by the claude the walk
# meets: that planted claude, or a real session in the runner's ancestry, would
# otherwise be terminated in its place.
done = run(panes=None, claude_pid=STRANGER.pid)
check("$CLAUDE_PID naming a non-claude process is refused, not walked past",
      done.returncode == NO_TRANSPORT_RC
      and "the ancestry walk never passed $CLAUDE_PID" in done.stderr
      and f"claude_pid={done.root_pid} " not in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

def refused_as_not_a_session(done):
    """The own claude, the planted hop, was refused as no session to relaunch."""
    return (done.returncode == NO_TRANSPORT_RC
            and f"claude process {done.root_pid} is not an interactive session" in done.stderr
            and f"claude_pid={done.root_pid} " not in done.stdout)


# A `--print` claude is a headless SDK bridge with no input box, so a fresh tmux
# window cannot stand in for it. Relaunching it interactively would sever the remote
# client driving it. The command line is forged onto the planted hop, which is the own
# claude, so it is refused rather than exchanged for a claude further up.
done = run(panes=None, as_claude=(
    "/x/claude/versions/2.1.283 --print --sdk-url https://api.anthropic.com/v1/code/sessions/cse_1"
    " --session-id cse_1 --input-format stream-json --model claude-opus-5-5").split())
check("a headless (--print) claude is not a session to relaunch",
      refused_as_not_a_session(done),
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The daemon's pty host runs the same binary. Terminating it would take every session
# it hosts, so it is refused by its flags.
done = run(panes=None, as_claude=(
    "/x/claude --bg-pty-host /tmp/cc-daemon/pty/a.sock 148 38 --"
    " /x/claude/versions/2.1.283 --session-id a --permission-mode bypassPermissions").split())
check("the daemon's --bg-pty-host process is not a session to relaunch",
      refused_as_not_a_session(done),
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# An unlisted long flag followed by a flag is skipped, so the scan still reaches the
# classifier behind it rather than calling the process an unreadable session.
for classifier in ("--print", "--bg-pty-host /tmp/cc-daemon/pty/a.sock"):
    done = run(panes=None, as_claude=f"/x/claude --frobnicate {classifier}".split())
    check(f"an unlisted flag before {classifier.split()[0]} does not make the process a session",
          refused_as_not_a_session(done),
          f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# An unlisted short flag may be a cluster hiding `-p` (`-cp` is `-c -p`), so it is
# refused even with a flag after it, never relaunched as an interactive session.
done = run(panes=None, as_claude="/x/claude -cp --model opus".split())
check("an unlisted short-flag cluster is refused, since it may hide -p",
      done.returncode == NO_TRANSPORT_RC and "cannot read the flags" in done.stderr
      and "'-cp'" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The headless check reads only the flag region. A session started from a prior
# handoff carries that handoff as its positional prompt, and the prompt can contain
# `--print` in prose.
done = run(panes=None, as_claude=[
    "/x/claude", "--permission-mode", "plan", "the ceiling hook ignores --print sessions; run /next"])
check("a prompt that mentions --print does not make the session headless",
      picked(done) == DETACHED and "flags=[--permission-mode plan]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The relaunching transports carry a requested model and effort as claude flags, after the
# old session's own flags so the request wins.
done = run(panes=None, as_claude="/x/claude --model sonnet --permission-mode plan".split(),
           launcher_flags=("--model", "opus[1m]", "--effort", "high"))
check("a detached relaunch starts on the requested model and effort",
      picked(done) == DETACHED
      and "flags=[--model sonnet --permission-mode plan --model opus\\[1m\\] --effort high]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
done = run(panes=None, as_claude="/x/claude --permission-mode plan".split())
check("and without a request the relaunch flags are only the old session's",
      "flags=[--permission-mode plan]" in done.stdout and "--effort" not in done.stdout,
      f"rc={done.returncode} out={done.stdout!r}")

# The relaunch line is parsed by a shell (tmux's default-shell, or the terminal's). An
# unquoted `--model opus[1m]` is a glob there, which zsh aborts on when nothing matches,
# and a quote in the handoff's path unbalances the line. The dry run prints the line the
# worker runs; it is checked through zsh and through bash's failglob and must yield the
# exact argv.
quoted_dir = tempfile.mkdtemp(prefix="o'neil [1m] ")
done = run(panes=None, handoff_dir=quoted_dir, as_claude=(
    "/x/claude --model opus[1m] --permission-mode bypassPermissions run /next").split())
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

# The flags a daemon-hosted session is launched with: harness flags before
# `--permission-mode`. Each flag before it must be consumed with its value, or the value
# is read as the prompt and the scan halts before the permission mode.
DAEMON_ARGV = ("/x/claude/versions/2.1.283 --session-id a00eaf96 --fork-session"
               " --resume /x/p.jsonl --allowed-tools mcp__a --allowed-tools mcp__b"
               " --permission-mode bypassPermissions")
done = run(panes=None, as_claude=DAEMON_ARGV.split())
check("a daemon-hosted session's harness flags are consumed and its permission mode kept",
      picked(done) == DETACHED and "flags=[--permission-mode bypassPermissions]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# argv is read element by element, so a flag value with a space is one value. Read as
# ps joins it, `--name review worker` gave the flag `review`, made `worker` the prompt,
# and the scan stopped before the permission mode, relaunching without it. An empty
# value is one element too, not a gap that lets the flag swallow the next one.
for spaced in (["--name", "review worker"], ["--append-system-prompt", "be terse; cite files"],
               ["--name", ""]):
    done = run(panes=None, as_claude=["/x/claude", *spaced, "--permission-mode", "bypassPermissions"])
    check(f"a flag value of {spaced[1]!r} before --permission-mode is one value, and the relaunch keeps the mode",
          picked(done) == DETACHED and "flags=[--permission-mode bypassPermissions]" in done.stdout,
          f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# argv0 is the first element whole, so a claude installed under a path with a space is
# still a claude binary. Planted by `exec -a` alone, this argv0 is the kernel's own read.
SPACED_CLAUDE = os.path.join(FIXTURES, "Application Support", "Claude", "claude-code", "2.1.300", "claude")
done = run(panes=None, as_claude=SPACED_CLAUDE)
check("a claude installed under a path with a space is found by its argv0",
      picked(done) == DETACHED and f"claude_pid={done.root_pid} " in done.stdout
      and identity_names(done, SPACED_CLAUDE),
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# Each flag claude takes is dropped with the number of values its arity specifies, so
# the flag that matters is still reached. The prompt here is a recap dense with dash
# tokens, each word its own element as a prompt typed unquoted would be, and none of
# them may be read as a flag.
done = run(panes=None, as_claude=(
    "/x/claude -c --add-dir /a /b --allowed-tools Read Edit --debug api -w --effort high"
    " --permission-mode plan === recap === - shipped --print support - fixed"
    " --permission-mode handling - never --dangerously-skip-permissions").split())
check("variadic and boolean flags are dropped by arity and the scan halts at the prompt",
      picked(done) == DETACHED and "flags=[--permission-mode plan]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A session /background forked carries the hidden `--reply-on-resume` (boolean) and
# `--proactivity <level>`, which `claude --help` does not list.
done = run(panes=None, as_claude=(
    "/x/claude/versions/2.1.299 --resume /x/p.jsonl --fork-session --reply-on-resume"
    " --allowed-tools mcp__a --model opus --effort high --permission-mode bypassPermissions"
    " --proactivity high --name worker").split())
check("a session forked by /background (--reply-on-resume, --proactivity) relaunches with its model and permission mode",
      picked(done) == DETACHED and "flags=[--model opus --permission-mode bypassPermissions]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# Boolean by the table, not by the token after it: followed by the prompt it is still
# no value, and the scan stops at the prompt.
done = run(panes=None, as_claude=(
    "/x/claude/versions/2.1.299 --permission-mode plan --reply-on-resume resume the review").split())
check("--reply-on-resume before the prompt is read as boolean, not refused",
      picked(done) == DETACHED and "flags=[--permission-mode plan]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A flag the table does not list has no value when the token after it is a flag or
# there is none, whatever its arity, so it is skipped.
done = run(panes=None, as_claude="/x/claude --frobnicate --permission-mode plan --frobnicate".split())
check("an unlisted flag followed by a flag, or last, is skipped and the permission mode kept",
      picked(done) == DETACHED and "flags=[--permission-mode plan]" in done.stdout,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# Followed by anything else, whether that token is its value or the prompt cannot be
# determined. The launcher refuses, names the flag and the token, and leaves the handoff
# on disk rather than guessing.
done = run(panes=None, as_claude="/x/claude --frobnicate maybe --permission-mode plan".split())
check("an unlisted flag followed by a non-flag is a refusal naming both, not a guess at the prompt",
      done.returncode == NO_TRANSPORT_RC and "cannot read the flags" in done.stderr
      and "--frobnicate" in done.stderr and "'maybe'" in done.stderr,
      f"rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# The daemon itself: a subcommand where a session's prompt would be. It is refused
# like the pty host it spawns.
done = run(panes=None, as_claude="/x/claude daemon run".split())
check("a claude subcommand (daemon run) is not a session to relaunch",
      refused_as_not_a_session(done),
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

# A pre-warmed spare, as one runs live: its subcommand spelling must not be read
# as the prompt.
done = run(panes=None, as_claude="/x/claude bg-spare --bg-spare /tmp/cc/spare/b3.claim.sock".split())
check("a daemon spare (bg-spare --bg-spare) is not a session to relaunch",
      refused_as_not_a_session(done),
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
# A host subcommand counts only with its own follow-up token; a prompt that merely
# starts with the word is a session's prompt.
done = run(panes=None, as_claude="/x/claude --permission-mode plan bg-pty-host leaks are fixed".split())
check("a prompt starting with a host word is still a session",
      picked(done) == DETACHED and f"claude_pid={done.root_pid} " in done.stdout,
      f"root_pid={done.root_pid} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")

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
done = run(tmux_on_path=False, outer_host=True)
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
# with or without $CLAUDE_PID naming it, and the refusal says why.
for export in (False, True):
    done = run(depth=2, sleep=2, forge_age="00:00", panes=None, export_claude_pid=export)
    check(f"a claude at a reissued pid is never relaunched as this session's own "
          f"({'with' if export else 'without'} $CLAUDE_PID)",
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
