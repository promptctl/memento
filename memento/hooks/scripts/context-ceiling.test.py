#!/usr/bin/env python3
"""Unit tests for the context-ceiling hook.

Each case feeds a synthetic transcript and the payload Claude Code actually delivers, to
the hook as a subprocess, and asserts the JSON it emits. [LAW:behavior-not-structure] it is
driven the way the harness drives it, so a rewrite of the internals cannot break these.

Run: python3 context-ceiling.test.py
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# One scratch root for every mkdtemp() below, removed on exit - each call still gets its own
# subdirectory, but the suite no longer abandons one per case in the system temp dir.
SCRATCH = tempfile.mkdtemp(prefix="context-ceiling-test-")
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)


def scratch_dir():
    return tempfile.mkdtemp(dir=SCRATCH)


HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "context-ceiling.py")
PLUGIN = os.path.dirname(os.path.dirname(HERE))
LIB = os.path.join(PLUGIN, "lib")
LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        "skills", "message-in-a-bottle", "bin", "finalize-session")
# The skill the block instruction tells the agent to load before running the close-out. It
# describes the launcher's report, so its prose reads like that report without being one.
CONTRACT = os.path.join(os.path.dirname(os.path.dirname(LAUNCHER)), "SKILL.md")
# [LAW:one-source-of-truth] every case drives the threshold explicitly, so the shipped
# default lives in ceiling_config alone and retuning it cannot break these. A fixture
# magnitude, not a second copy of that number.
TEST_CEILING = 100_000
USER_CEILING = f"ceiling = {TEST_CEILING}\n"
OVER, UNDER = TEST_CEILING + 20_000, TEST_CEILING - 60_000
SESSION = "s-1"
# The file names come from the module the hook itself reads them from, so a fixture here cannot
# drift from the files the hook looks for. [LAW:one-source-of-truth]
sys.path.insert(0, LIB)
from ceiling_config import CONFIG_NAME, DEFAULT_CEILING, GRACE  # noqa: E402

CLEAR_HOOK = os.path.join(HERE, "clear-session-ceiling.py")

# Past the ceiling and past the limit the grace sets beyond it: the two bands a stop can block in.
LIMIT = TEST_CEILING + GRACE
PAST_LIMIT = LIMIT + 20_000

failures = []


def write_conf(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    return path


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def assistant(tokens, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "message": {"usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": tokens - 2, "output_tokens": 0}}}


def tool_use(name, tool_input, call_id="t-1", sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]}}


# The launcher's own report of a scheduled reset is what credits a close-out, so a result
# carries real launcher output by default and `out` only where the call is not one.
SCHEDULED = "handoff scheduled \u2192 tmux memento:1.0 in 10s (log: /tmp/l)"


def tool_result(call_id="t-1", is_error=False, content="out", sidechain=False):
    block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "isSidechain": sidechain, "message": {"content": [block]}}


user = {"type": "user", "isSidechain": False, "message": {"content": "hi"}}


def run(records, event="Stop", stop_hook_active=False,
        hook=HOOK, user_conf=USER_CEILING, project_conf=None, session_conf=None,
        project=None, config_home=None, xdg=None, log_seed="", extra_env=None,
        session=SESSION):
    """Invoke the hook as Claude Code does. Returns (exit code, parsed stdout, stderr).

    The threshold is written where a person writes one, so the suite drives the hook through
    the surface it ships. user_conf=None writes no file at all, which is how a case reaches the
    shipped default. Every config layer is rooted in scratch and every ambient one is stripped:
    a developer's own ceiling, project or CLAUDE_PROJECT_DIR must not decide a test."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    handle.write("".join(json.dumps(r) + "\n" for r in records))
    handle.close()
    log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    log.write(log_seed)
    log.close()
    home = config_home or (os.path.join(xdg, "promptctl") if xdg else scratch_dir())
    project = project or scratch_dir()
    if user_conf is not None:
        write_conf(os.path.join(home, CONFIG_NAME), user_conf)
    if session_conf is not None:
        write_conf(os.path.join(home, "sessions", session, CONFIG_NAME), session_conf)
    if project_conf is not None:
        write_conf(os.path.join(project, ".promptctl", CONFIG_NAME), project_conf)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "XDG_CONFIG_HOME")}
    env["MEMENTO_CEILING_LOG"] = log.name
    # xdg drives the shipped path construction rather than overriding it, which is the only
    # way to exercise where the config really lives without reading the developer's own.
    if xdg is None:
        env["MEMENTO_CONFIG_HOME"] = home
    else:
        env["XDG_CONFIG_HOME"] = xdg
    env.update(extra_env or {})
    payload = {"session_id": session, "hook_event_name": event, "cwd": project,
               "transcript_path": handle.name, "stop_hook_active": stop_hook_active}
    try:
        done = subprocess.run([sys.executable, hook], text=True, capture_output=True,
                              env=env, input=json.dumps(payload))
    finally:
        os.unlink(handle.name)
    out = json.loads(done.stdout) if done.stdout.strip() else None
    run.log = open(log.name).read()
    os.unlink(log.name)
    return done.returncode, out, done.stderr



# --- Stop -------------------------------------------------------------------------------

code, out, _ = run([user, assistant(UNDER)])
check("under the ceiling, the stop is allowed", code == 0 and out is None, f"{code} {out}")

code, out, _ = run([user, assistant(OVER)])
check("over the ceiling, the stop is blocked", out and out.get("decision") == "block", f"{code} {out}")
check("the reason names the launcher, the count and the ceiling",
      out and LAUNCHER in out["reason"] and f"{OVER:,}" in out["reason"]
      and f"{TEST_CEILING:,}" in out["reason"], str(out))
# A worktree-isolated session may be refused the command above by the platform, which deleting
# this hook's own gate did nothing to change.
check("the reason tells a worktree session how to reach a directory it can run from",
      out and "ExitWorktree" in out["reason"], str(out))

code, out, _ = run([user, assistant(PAST_LIMIT)], stop_hook_active=True)
check("a stop already blocked once is not blocked again",
      code == 0 and out and "decision" not in out, f"{code} {out}")
check("giving up is loud rather than silent",
      out and "context ceiling breached" in out.get("systemMessage", ""), str(out))
check("giving up does not claim the close-out failed",
      out and "If the close-out did not run" in out.get("systemMessage", "")
      and "was NOT closed out" not in out.get("systemMessage", ""), str(out))
check("a spent forced close-out is logged apart from a spent finishing continuation",
      "-> spent-block" in run.log, run.log)

# --- the grace: a unit in progress is finished before the close-out ---------------------------

# Between the ceiling and the limit the stop is still blocked once, because the agent has to be told;
# what it is told is to finish the unit it is in and close out after, not to drop it where it stands.
code, out, _ = run([user, assistant(OVER)])
check("past the ceiling but under the limit, the agent may finish its unit",
      out and out.get("decision") == "block" and "finish" in out["reason"].lower()
      and f"{LIMIT:,}" in out["reason"] and "Close it out now" not in out["reason"], str(out))
check("and a unit already finished is still closed out through the same command",
      out and LAUNCHER in out["reason"] and "ExitWorktree" in out["reason"], str(out))
check("the finishing block forbids raising the ceiling to make room",
      out and "move the ceiling" in out["reason"], str(out))
check("the finishing band is logged apart from the forced close-out", "-> finish" in run.log, run.log)
code, out, _ = run([user, assistant(PAST_LIMIT)])
check("past the limit, the close-out is due now whatever is in progress",
      out and out.get("decision") == "block" and "Close it out now" in out["reason"], str(out))
check("the forced close-out block forbids raising the ceiling too",
      out and "move the ceiling" in out["reason"], str(out))
code, out, _ = run([user, assistant(LIMIT)])
check("a session at exactly the limit is past it",
      out and "Close it out now" in out["reason"], str(out))
code, out, _ = run([user, assistant(LIMIT - 1)])
check("a session one token below the limit is still finishing",
      out and "Close it out now" not in out["reason"], str(out))
# An agent mid-unit that ends its turn to wait on something is let through the second stop, and the
# person watching is not told the next session starts with nothing, because nothing was forced.
code, out, _ = run([user, assistant(OVER)], stop_hook_active=True)
check("a second stop while finishing is let through without a breach alarm",
      code == 0 and out and "decision" not in out
      and "context ceiling breached" not in out.get("systemMessage", "")
      and f"{LIMIT:,}" in out.get("systemMessage", ""), str(out))
check("a spent finishing continuation is logged under its own label",
      "-> spent-finish" in run.log, run.log)
# A finishing block restarts the turn, and the agent then works through its unit inside that turn, so
# the stop that ends it arrives marked as following a block. Past the limit, that stop is the one
# the limit exists for. The block reaches the transcript as the harness writes it, and the case below
# builds it from the hook's own output.
_, finishing, _ = run([user, assistant(OVER)])
_, closing, _ = run([user, assistant(PAST_LIMIT)])


def fed_back(verdict):
    return {"type": "user", "isSidechain": False,
            "message": {"role": "user", "content": f"Stop hook feedback:\n{verdict['reason']}"}}


code, out, _ = run([user, assistant(OVER), fed_back(finishing), assistant(PAST_LIMIT)],
                   stop_hook_active=True)
check("a turn a finishing block started is still stopped at the limit",
      out and out.get("decision") == "block" and "Close it out now" in out["reason"], str(out))
code, out, _ = run([user, assistant(OVER), fed_back(finishing), assistant(OVER + 10_000)],
                   stop_hook_active=True)
check("while under the limit that turn's stop is let through",
      code == 0 and out and "decision" not in out, str(out))
code, out, _ = run([user, assistant(PAST_LIMIT), fed_back(closing), assistant(PAST_LIMIT + 5_000)],
                   stop_hook_active=True)
check("a closing block is never followed by a second one",
      code == 0 and out and "decision" not in out
      and "context ceiling breached" in out.get("systemMessage", ""), str(out))
code, out, _ = run([user, assistant(OVER), fed_back(finishing), assistant(PAST_LIMIT),
                    fed_back(closing), assistant(PAST_LIMIT + 5_000)], stop_hook_active=True)
check("and the escalation happens once, not at every stop past the limit",
      code == 0 and out and "decision" not in out, str(out))
# A user record's content is a string or a list of text blocks; the finishing block reaches the
# transcript as either, and the band is read off its text the same way, so the escalation fires
# whichever shape the harness wrote.
def fed_back_blocks(verdict):
    return {"type": "user", "isSidechain": False, "message": {"role": "user",
            "content": [{"type": "text", "text": f"Stop hook feedback:\n{verdict['reason']}"}]}}
code, out, _ = run([user, assistant(OVER), fed_back_blocks(finishing), assistant(PAST_LIMIT)],
                   stop_hook_active=True)
check("a finishing block fed back as list content still escalates at the limit",
      out and out.get("decision") == "block" and "Close it out now" in out["reason"], str(out))

# The limit rides on the ceiling rather than being set beside it, so a ceiling switched off has none.
code, out, _ = run([user, assistant(5_000_000)], user_conf="ceiling = off\n")
check("a ceiling switched off has no limit either", code == 0 and out is None, f"{code} {out}")

ran_closeout = [user, assistant(OVER),
                tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}),
                tool_result(content=SCHEDULED)]
code, out, _ = run(ran_closeout)
check("a stop right after the close-out ran is allowed",
      code == 0 and out and "decision" not in out
      and "the close-out ran" in out.get("systemMessage", ""), str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}),
                    tool_result(is_error=True, content=SCHEDULED)])
check("a close-out that was refused does not count as having run",
      out and out.get("decision") == "block", str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": "git status"}), tool_result()])
check("a call that is not the close-out does not count as one",
      out and out.get("decision") == "block", str(out))
# The instruction hands out an absolute path, but an agent holding the skill may reach the
# launcher by name, or through a wrapper. What it was called does not decide this; what it did.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": "finalize-session 'bye'"}),
                    tool_result(content=SCHEDULED)])
check("a bare-name close-out is credited at the stop",
      code == 0 and out and "decision" not in out
      and "the close-out ran" in out.get("systemMessage", ""), str(out))
# A command string that names the launcher without running it.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command":
                        "echo 'reminder: run finalize-session before stopping'"}),
                    tool_result(content="reminder: run finalize-session")])
check("a command that only mentions the close-out is not credited as running it",
      out and out.get("decision") == "block", str(out))
# The two ways a result lied, and both were reachable by following the block's own instruction:
# it says to load the close-out contract, and an agent that then wants to know what the launcher
# does reads its source. Each text says `handoff scheduled` about the launcher without being the
# launcher, so the words alone credited a close-out that never ran.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Skill", {"skill": "memento:message-in-a-bottle"}),
                    tool_result(content=open(CONTRACT).read())])
check("loading the close-out contract is not running the close-out",
      out and out.get("decision") == "block", str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"cat {LAUNCHER}"}),
                    tool_result(content=open(LAUNCHER).read())])
check("reading the launcher's source is not running the launcher",
      out and out.get("decision") == "block", str(out))
# Bash is the one tool whose output the agent writes, by choosing the command that prints it, so
# a rendered line proves nothing on its own. Naming the launcher is what this call cannot do
# without saying out loud what it is imitating.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"echo '{SCHEDULED}'"}),
                    tool_result(content=SCHEDULED)])
check("a command that echoes the launcher's line is not the launcher",
      out and out.get("decision") == "block", str(out))
# Naming the launcher in the same breath does not rescue it. A real invocation cannot carry the
# report it produces - the log path is made by mktemp while it runs - so the line's presence in
# the command is what separates printing it from causing it.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command":
                        f"echo 'Reminder to run finalize-session next time. {SCHEDULED}'"}),
                    tool_result(content=f"Reminder to run finalize-session next time. {SCHEDULED}")])
check("a reminder naming the launcher and quoting its line is not a close-out",
      out and out.get("decision") == "block", str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"printf '%s\\n' '{SCHEDULED}' # {LAUNCHER}"}),
                    tool_result(content=SCHEDULED)])
check("printing the line from a command that names the launcher is not a close-out",
      out and out.get("decision") == "block", str(out))
# finalize-session's own contract allows backticks/$ in a message, which the deleted parser
# choked on; a real, successful close-out written that way must still be credited.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command":
                        f'{LAUNCHER} "See `git rev-parse HEAD`"'}),
                    tool_result(content=SCHEDULED)])
check("a close-out containing shell-special characters is credited if it ran",
      code == 0 and out and "decision" not in out
      and "the close-out ran" in out.get("systemMessage", ""), str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}, call_id="a"),
                    tool_result("a", content=SCHEDULED),
                    tool_use("Bash", {"command": "git push"}, call_id="b"),
                    tool_result("b")])
check("a confirming git call after the close-out does not undo it",
      code == 0 and out and "decision" not in out
      and "the close-out ran" in out.get("systemMessage", ""), str(out))
# The tmux transport clears in place, so the transcript keeps growing past a close-out.
# Crediting that one forever would wave through every later breach in the same file.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}),
                    tool_result(content=SCHEDULED),
                    user, assistant(OVER)])
check("a close-out from an earlier turn does not excuse a later breach",
      out and out.get("decision") == "block", str(out))

# --- the ceiling is configurable while sessions run --------------------------------------

def blocked_at(out, ceiling):
    """Blocked on this ceiling, matched as the phrase naming it: the reason also states the limit, and
    a bare number would let a wrong ceiling whose limit happens to be this number pass."""
    return (bool(out) and out.get("decision") == "block"
            and f"past the {ceiling:,} ceiling" in out.get("reason", ""))


_, out, _ = run([user, assistant(60_000)], user_conf="ceiling = 50000\n")
check("a user config is honoured", blocked_at(out, 50_000), str(out))
_, out, _ = run([user, assistant(60_000)], project_conf="ceiling = 50000\n")
check("a project config is honoured", blocked_at(out, 50_000), str(out))
_, out, _ = run([user, assistant(60_000)], session_conf="ceiling = 50000\n")
check("a session config is honoured", blocked_at(out, 50_000), str(out))
# Through XDG rather than the override, so the path people actually write to is the one under
# test rather than a path only the suite ever uses.
_, out, _ = run([user, assistant(60_000)], xdg=scratch_dir(),
                user_conf="ceiling = 50000\n")
check("the user config is read from $XDG_CONFIG_HOME/promptctl", blocked_at(out, 50_000), str(out))

_, out, _ = run([user, assistant(60_000)], user_conf="ceiling = 20000\n",
                project_conf="ceiling = 50000\n")
check("the project outranks the user config", blocked_at(out, 50_000), str(out))
_, out, _ = run([user, assistant(60_000)], project_conf="ceiling = 20000\n",
                session_conf="ceiling = 50000\n")
check("the session outranks the project config", blocked_at(out, 50_000), str(out))

# The whole point of the exercise: one session delays its own handoff without editing, or
# even knowing, the number the project pinned.
_, out, _ = run([user, assistant(400_000)], project_conf="ceiling = 250000\n",
                session_conf="ceiling = +100_000\n")
check("a session adjustment moves the ceiling the project pinned", blocked_at(out, 350_000), str(out))
_, out, _ = run([user, assistant(400_000)], project_conf="ceiling = 250000\n",
                session_conf="ceiling = -50000\n")
check("an adjustment can lower the ceiling too", blocked_at(out, 200_000), str(out))
_, out, _ = run([user, assistant(DEFAULT_CEILING + 60_000)], user_conf="ceiling = +30000\n",
                project_conf="ceiling = +20000\n")
check("adjustments at two layers both apply, in order",
      blocked_at(out, DEFAULT_CEILING + 50_000), str(out))
code, out, _ = run([user, assistant(5_000_000)], user_conf="ceiling = off\n",
                   session_conf="ceiling = +10000\n")
check("adjusting a ceiling that is switched off leaves it off", code == 0 and out is None, f"{code} {out}")
_, out, _ = run([user, assistant(400_000)], user_conf="ceiling = off\n",
                session_conf="ceiling = 300000\n")
check("a later absolute value overrules an earlier off", blocked_at(out, 300_000), str(out))

for word in ("off", "OFF"):
    code, out, _ = run([user, assistant(5_000_000)], user_conf=f"ceiling = {word}\n")
    check(f"the gate can be switched off by writing {word!r}",
          code == 0 and out is None, f"{code} {out}")

# Found by walking, so a subdirectory, a package, or a worktree inherits the repo above it.
root = scratch_dir()
write_conf(os.path.join(root, ".promptctl", CONFIG_NAME), "ceiling = 50000\n")
deep = os.path.join(root, "packages", "worker")
os.makedirs(deep)
_, out, _ = run([user, assistant(60_000)], project=deep)
check("a project config is found from a subdirectory of the project", blocked_at(out, 50_000), str(out))

# The project is the session's, not wherever a Bash call last left the shell.
elsewhere = scratch_dir()
_, out, _ = run([user, assistant(60_000)], project=elsewhere,
                extra_env={"CLAUDE_PROJECT_DIR": root})
check("CLAUDE_PROJECT_DIR anchors the project config, not the payload's cwd",
      blocked_at(out, 50_000), str(out))
code, out, _ = run([user, assistant(60_000)], project_conf="ceiling = 50000\n",
                   extra_env={"CLAUDE_PROJECT_DIR": elsewhere})
check("a cwd config is not read when CLAUDE_PROJECT_DIR points somewhere else",
      code == 0 and out is None, f"{code} {out}")

# A project directory holding the user config would otherwise apply one file as two layers.
shared = scratch_dir()
code, out, _ = run([user, assistant(400_000)], project=shared,
                   config_home=os.path.join(shared, ".promptctl"),
                   user_conf="ceiling = +10000\n")
check("the user config is not applied a second time as the project config",
      blocked_at(out, DEFAULT_CEILING + 10_000), str(out))

# --- the ceiling is a live read of the layers at every stop ---------------------------------

# Nothing is frozen per session. A shared layer edited, deleted or created under a running session is
# what that session is gated on at its next stop - delivered as a close-out instruction, since Stop is
# the only event this runs on, never as a denial. Each case runs one session more than once against one
# config home, because a second stop seeing the change is the whole of what is under test.

home = scratch_dir()
code, out, _ = run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
check("a session's first stop passes under the shared ceiling as it stands",
      code == 0 and out is None, f"{code} {out}")
_, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 250000\n")
check("a shared ceiling lowered under a running session instructs it to close out at its next stop",
      blocked_at(out, 250_000), str(out))
check("and what it gets is the finishing instruction, not a denial",
      out and out.get("decision") == "block" and "finish" in out["reason"].lower(), str(out))
code, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 450000\n")
check("a shared ceiling raised under a running session lets its next stop through",
      code == 0 and out is None, f"{code} {out}")

# A deletion is a change like any other: the session falls to the layer beneath.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 250000\n")
os.unlink(os.path.join(home, CONFIG_NAME))
_, out, _ = run([user, assistant(DEFAULT_CEILING + 10_000)], config_home=home, user_conf=None)
check("a shared ceiling deleted under a running session hands it to the layer beneath",
      blocked_at(out, DEFAULT_CEILING), str(out))
# And so is a project file appearing where none stood.
home, repo = scratch_dir(), scratch_dir()
run([user, assistant(1_000)], config_home=home, project=repo)
_, out, _ = run([user, assistant(300_000)], config_home=home, project=repo,
                project_conf="ceiling = 250000\n")
check("a project ceiling written under a running session reaches it at its next stop",
      blocked_at(out, 250_000), str(out))

# Between stops the hook writes nothing but the log: the sessions tree holds only what `ceiling set
# session` puts there, so there is no per-session record to go stale.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 350000\n")
check("a session that only stops leaves nothing under the sessions tree",
      not os.path.exists(os.path.join(home, "sessions")),
      str(os.path.exists(os.path.join(home, "sessions")) and os.listdir(os.path.join(home, "sessions"))))

# The session's own layer moves the ceiling immediately, in both directions - which is what makes the
# block escapable from inside the block - and it survives an ordinary stop untouched.
home = scratch_dir()
code, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 250000\n",
                   session_conf="ceiling = +100_000\n")
check("a session layer written mid-session raises the ceiling on the very next stop",
      code == 0 and out is None, f"{code} {out}")
_, out, _ = run([user, assistant(200_000)], config_home=home, user_conf="ceiling = 250000\n",
                session_conf="ceiling = -100_000\n")
check("and lowers it on the next stop too - no direction test, no asymmetry",
      blocked_at(out, 150_000), str(out))
override = os.path.join(home, "sessions", SESSION, CONFIG_NAME)
check("an ordinary stop leaves the session override standing",
      open(override).read() == "ceiling = -100_000\n", str(os.path.exists(override)))
os.unlink(override)
_, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 200000\n")
check("deleting the session layer hands the session to the shared layers as they stand now",
      blocked_at(out, 200_000), str(out))


# --- a session override resets with the session, /clear included ------------------------------

# A kill-and-relaunch reset gets a new session id and so drops its override by construction. `/clear`
# keeps the id, so a SessionStart hook matched to it removes the file, and the successor context runs
# under the user and project layers until it sets its own.

def cleared(home, session=SESSION, source="clear", event="SessionStart", raw=None):
    """Invoke the SessionStart hook as Claude Code does on /clear. Returns (exit code, stderr)."""
    log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    log.close()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "XDG_CONFIG_HOME")}
    env.update({"MEMENTO_CONFIG_HOME": home, "MEMENTO_CEILING_LOG": log.name})
    payload = raw if raw is not None else json.dumps(
        {"session_id": session, "hook_event_name": event, "source": source, "cwd": home})
    done = subprocess.run([sys.executable, CLEAR_HOOK], text=True, capture_output=True, env=env,
                          input=payload)
    cleared.log = open(log.name).read()
    os.unlink(log.name)
    return done.returncode, done.stderr


home = scratch_dir()
_, out, _ = run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 250000\n",
                session_conf="ceiling = 500000\n")
check("before the reset, the session override is what gates the session", out is None, str(out))
code, err = cleared(home)
check("the /clear hook exits clean", code == 0 and not err, f"{code} {err}")
check("and the session override is gone, and the directory that held only it",
      not os.path.exists(os.path.join(home, "sessions", SESSION)),
      str(os.path.exists(os.path.join(home, "sessions", SESSION))
          and os.listdir(os.path.join(home, "sessions", SESSION))))
check("and says so in the ceiling log", "-> dropped" in cleared.log and "SessionStart" in cleared.log,
      cleared.log)
_, out, _ = run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 250000\n")
check("so the post-clear context is gated on the user and project layers",
      blocked_at(out, 250_000), str(out))
code, err = cleared(home)
check("a /clear in a session that set no override is not an error", code == 0 and not err, f"{code} {err}")
check("and the log tells that apart from a dropped override", "-> no-override" in cleared.log, cleared.log)

# A drifted hooks.json is the way this hook would fire on the wrong event and drop an override the
# session still means to have. Any payload that is not a /clear stops it, and the stop is recorded.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, session_conf="ceiling = 500000\n")
for event, source in (("SessionStart", "compact"), ("SessionStart", "startup"), ("Stop", "clear")):
    code, err = cleared(home, event=event, source=source)
    check(f"a {event} payload with source {source!r} stops the hook rather than dropping the override",
          code == 1 and "hooks.json" in err
          and os.path.exists(os.path.join(home, "sessions", SESSION, CONFIG_NAME)), f"{code} {err}")
    check("and the stop is recorded", "-> stopped" in cleared.log, cleared.log)
# A real Stop payload carries no `source` at all; the refusal has to be the curated one, not a
# KeyError raised while composing it.
code, err = cleared(home, raw=json.dumps({"session_id": SESSION, "hook_event_name": "Stop"}))
check("a real Stop payload, which has no source, is refused in the hook's own voice",
      code == 1 and "hooks.json" in err and "Traceback" not in err, f"{code} {err}")
for raw in ("42", "[]", "not json", "{}"):
    code, err = cleared(home, raw=raw)
    check(f"a /clear payload of {raw!r} stops the hook loudly and is recorded",
          code == 1 and "-> stopped" in cleared.log, f"{code} {err} | {cleared.log!r}")
code, err = cleared(home, session="../..")
check("a session id that is not a bare name is refused by the /clear hook too",
      code == 1 and "session directory" in err, f"{code} {err}")

# --- a setting nobody can misspell into silence -------------------------------------------

code, out, err = run([user, assistant(OVER)], user_conf="ceiling = 350k\n")
check("a ceiling that does not parse fails loudly, naming the file and the line",
      code == 1 and "350k" in err and "line 1" in err and CONFIG_NAME in err, f"{code} {err}")
code, out, err = run([user, assistant(OVER)], user_conf="ceilling = 350000\n")
check("a misspelled key fails loudly rather than reading as a setting nobody made",
      code == 1 and "ceilling" in err and "ceiling" in err, f"{code} {err}")
# The loud stderr is not enough: a stopped Stop hook is non-blocking, so this session now runs
# with no ceiling, and stderr scrolls away. The log is where that has to be legible after.
check("and the stopped gate leaves a durable log line, not only stderr",
      "-> stopped" in run.log and "ceilling" in run.log, run.log)
# Bytes that are not text are the one shape of "not this format" that used to arrive as a traceback,
# and a traceback out of a Stop hook is a gate Claude Code treats as non-blocking: off, with nothing
# anywhere stating why.
bytes_home = scratch_dir()
with open(os.path.join(bytes_home, CONFIG_NAME), "wb") as handle:
    handle.write(b"ceiling = 35\xff0000\n")
code, out, err = run([user, assistant(400_000)], config_home=bytes_home, user_conf=None)
check("a shared file of bytes that are not text fails in the parser's own voice",
      code == 1 and "not text" in err and "Traceback" not in err, f"{code} {err}")
# The key this setting used to be spelled with is a rejection, not a synonym. Every other
# case here writes the current spelling, so nothing else in the suite would notice an alias
# readmitted for compatibility. The new key is a substring of the retired one, so the
# message is asked for both: the key refused, and the key that is legal.
code, out, err = run([user, assistant(OVER)], user_conf="context_ceiling = 350000\n")
check("the retired key is refused rather than read as a synonym",
      code == 1 and "context_ceiling" in err and "It reads: ceiling" in err and "line 1" in err,
      f"{code} {err}")
# Every upgrade path from a machine that ever set a ceiling runs through this key, and rejecting it
# stops the hook before the ceiling is resolved. The log has to record that stop and name the key,
# or a machine gated off by a stale config looks exactly like one no session has crossed.
check("a session stopped by a rejected key records the stop and the key in the log",
      "-> stopped" in run.log and "context_ceiling" in run.log, run.log)
code, out, err = run([user, assistant(OVER)], user_conf="ceiling 350000\n")
check("a line with no `=` fails loudly",
      code == 1 and "key = value" in err, f"{code} {err}")
code, out, err = run([user, assistant(OVER)], user_conf="ceiling =\n")
check("a key written with no value fails loudly, unlike an exported-empty variable",
      code == 1 and "key = value" in err, f"{code} {err}")
code, out, err = run([user, assistant(OVER)],
                     user_conf="ceiling = 300000\nceiling = 400000\n")
check("one key set twice in one file fails loudly",
      code == 1 and "twice" in err and "line 2" in err, f"{code} {err}")
# The shared fold is the one that gets written down, so it is the one that must not be allowed to
# resolve negative: `-50000` recorded is `-50000` read back as an *adjustment*, which resolves to
# a positive 200,000 nobody set and never trips the check below. Caught in review; the exit had
# stopped firing for this input entirely, on the recording stop as well as every later one.
code, out, err = run([user, assistant(OVER)],
                     user_conf=f"ceiling = -{DEFAULT_CEILING + 50_000}\n")
check("a shared fold that resolves below zero fails loudly rather than being recorded",
      code == 1 and "never negative" in err and "-50,000" in err, f"{code} {err}")
check("and it names the shared file that caused it",
      code == 1 and CONFIG_NAME in err, f"{code} {err}")

code, out, err = run([user, assistant(OVER)], user_conf="ceiling = 10000\n",
                     session_conf="ceiling = -50000\n")
# Both layers by path: the user file and the session's, which share a filename and nothing else.
check("adjustments that resolve below zero fail loudly, naming both layers",
      code == 1 and "never negative" in err and f"sessions/{SESSION}/{CONFIG_NAME}" in err
      and err.count(CONFIG_NAME) >= 2, f"{code} {err}")

# The disabling word is matched on what was written, not on what is left after the digit
# separators come out - `o_f_f` is a typo, and reading it as `off` would take the gate down
# by way of a misspelling, which is the one outcome this section exists to prevent.
code, out, err = run([user, assistant(5_000_000)], user_conf="ceiling = o_f_f\n")
check("'o_f_f' is a typo rather than a way to switch the gate off",
      code == 1 and "o_f_f" in err, f"{code} {err}")

# `str.isdigit` was true of these and `int` was not, so the crafted message was skipped and a
# traceback took its place. The shape the parse accepts and the one it converts are now one.
for exotic in ("\u00b2", "\u2075"):
    code, out, err = run([user, assistant(OVER)],
                         user_conf=f"ceiling = {exotic}\n")
    check(f"a digit-like character {exotic!r} that is not a number fails loudly, not by traceback",
          code == 1 and "should hold a number" in err and "Traceback" not in err, f"{code} {err}")

# Underscores group digits as Python's own literals do, so what a person writes in a config and
# what they would write in code are the same set - no more, and no less.
for malformed in ("_350000", "350000_", "3__50000", "+_100", "1_"):
    code, out, err = run([user, assistant(OVER)],
                         user_conf=f"ceiling = {malformed}\n")
    check(f"{malformed!r} is not a number of tokens", code == 1 and "should hold a number" in err,
          f"{code} {err}")
_, out, _ = run([user, assistant(400_000)], user_conf="ceiling = 3_5_0000\n")
check("underscores between digits group a number rather than breaking it",
      blocked_at(out, 350_000), str(out))

# The session id becomes a path exactly once, so that is where its shape is settled. An
# absolute id would discard the sessions directory outright; a relative one would climb out
# of it. Either reads a config no layer of this design points at.
for escape in ("/tmp", "../..", "a/b", "..", "./..", "..//", ".", "/"):
    code, out, err = run([user, assistant(OVER)], session=escape,
                         user_conf="ceiling = 300000\n")
    check(f"a session id of {escape!r} is refused rather than read as a directory",
          code == 1 and "session directory" in err, f"{code} {err}")

_, out, _ = run([user, assistant(60_000)],
                user_conf="# the handoff comes late on this machine\n\n"
                          "ceiling = 50000  # measured, not guessed\n")
check("comments and blank lines are not settings", blocked_at(out, 50_000), str(out))

# The shipped default, bracketed rather than named: a trivial session passes and one larger
# than any context window is caught, whatever the number happens to be.
code, out, _ = run([user, assistant(1_000)], user_conf=None)
check("with no config anywhere, a small session is allowed", code == 0 and out is None, f"{code} {out}")
_, out, _ = run([user, assistant(2_000_000)], user_conf=None)
check("with no config anywhere, a session past any window is blocked",
      out and out.get("decision") == "block", str(out))

# --- measuring the session ----------------------------------------------------------------

_, out, _ = run([user, {"type": "assistant", "isSidechain": False, "message": {"usage": {
    "input_tokens": 1_000, "cache_creation_input_tokens": 20_000,
    "cache_read_input_tokens": 70_000, "output_tokens": 12_000}}}])
check("the count sums input, cache write, cache read and output",
      out and "103,000" in out["reason"], str(out))

_, out, _ = run([user, assistant(OVER), assistant(20_000, sidechain=True)])
check("a trailing subagent record does not mask the session's count",
      out and out.get("decision") == "block", str(out))
code, out, _ = run([user, assistant(UNDER), assistant(900_000, sidechain=True)])
check("a subagent's context does not count against the session", code == 0 and out is None, str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}, sidechain=True),
                    tool_result(content=SCHEDULED, sidechain=True)])
check("a subagent running the launcher is not this session's close-out",
      out and out.get("decision") == "block", str(out))

code, out, _ = run([user, assistant(OVER), user, assistant(UNDER)])
check("a compacted session reads as its post-compaction size", code == 0 and out is None, str(out))

padding = [user] * 4000
code, out, _ = run(padding + [assistant(OVER)])
check("the newest record is found in a transcript spanning several chunks",
      out and out.get("decision") == "block", str(out))
# Wider than one backward read, so it is read whole only if the partial head is carried.
wide = assistant(OVER)
wide["pad"] = "x" * (400 * 1024)
code, out, _ = run(padding + [wide])
check("a record straddling a chunk boundary is still read whole",
      out and out.get("decision") == "block", str(out))
code, out, _ = run([assistant(OVER)] + padding + [user, assistant(UNDER)])
check("an older record beyond the first chunk does not override the newest",
      code == 0 and out is None, str(out))

code, out, _ = run([user, assistant(TEST_CEILING)])
check("a session at exactly the ceiling is over it, not under",
      out and out.get("decision") == "block", str(out))
code, out, _ = run([user, assistant(TEST_CEILING - 1)])
check("a session one token below the ceiling is under it", code == 0 and out is None, str(out))

code, out, _ = run([user], user_conf="ceiling = 1\n")
check("a transcript with no assistant record reads as zero", code == 0 and out is None, str(out))

# --- the log is the only place "allowed" and "never ran" differ ---------------------------

run([user, assistant(UNDER)])
check("an allowed call is logged too", "allow-under" in run.log, run.log)
run([user, assistant(PAST_LIMIT)])
check("a block is logged", "-> block" in run.log, run.log)
run([user, assistant(UNDER)], log_seed="old\n" * 600_000)
check("the log is truncated once it passes its cap",
      "[truncated at" in run.log and len(run.log) < 2_000_000, str(len(run.log)))

# --- wiring --------------------------------------------------------------------------------

isolated = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
isolated["MEMENTO_CONFIG_HOME"] = scratch_dir()
isolated["MEMENTO_CEILING_LOG"] = os.path.join(scratch_dir(), "log")
done = subprocess.run([sys.executable, HOOK], input="{}", text=True, capture_output=True,
                      env=isolated)
check("a payload with no event fails loudly",
      done.returncode == 1 and "hook_event_name" in done.stderr, str(done)[:200])
# Even a payload too malformed to name its event stops inside the guard, so the gate-off is
# recorded, not only thrown. This is the first isolated-env call, so the log holds just its line.
no_event_log = open(isolated["MEMENTO_CEILING_LOG"]).read()
check("and the stop on an unrecognisable payload is recorded, not only on stderr",
      "-> stopped" in no_event_log and "hook_event_name" in no_event_log, no_event_log)
# The count is only true at a stop, so being called anywhere else means hooks.json has drifted
# from this file. Measuring anyway is how a session gets denied on a number that is not its own.
code, out, err = run([user, assistant(OVER)], event="PreToolUse")
check("an event that is not Stop stops the hook rather than measuring",
      code == 1 and "PreToolUse" in err and "hooks.json" in err, f"{code} {err[:200]}")
# A drifted hooks.json fires this hook off Stop on every call, silently ungating every session; the
# stop belongs in the log for the same reason a rejected config does.
check("and a hook fired off Stop records the stop, not only on stderr",
      "-> stopped" in run.log and "PreToolUse" in run.log, run.log)
done = subprocess.run([sys.executable, HOOK], input='{"hook_event_name": "Stop"}', text=True,
                      capture_output=True, env=isolated)
check("a payload with no transcript_path fails loudly",
      done.returncode == 1 and "transcript_path" in done.stderr, str(done)[:200])
# The transcript is read before the ceiling is resolved, so a payload the hook stops on here dies
# even earlier than the no-cwd one - and it must leave the same durable record, or the gate goes
# off with only a traceback that scrolls away. The log is cumulative across the isolated env's
# earlier calls, so `transcript_path` - unique to this run - identifies its line.
stopped_early = open(isolated["MEMENTO_CEILING_LOG"]).read()
check("and a stop before the transcript is even read is recorded, not only on stderr",
      "-> stopped" in stopped_early and "transcript_path" in stopped_early, stopped_early)
# The project config is resolved from it, so a payload without it is a hook that would
# silently read no project config at all.
empty_transcript = write_conf(os.path.join(scratch_dir(), "t.jsonl"), "")
done = subprocess.run([sys.executable, HOOK], text=True, capture_output=True, env=isolated,
                      input=json.dumps({"hook_event_name": "Stop", "session_id": SESSION,
                                        "transcript_path": empty_transcript}))
check("a payload with no cwd fails loudly",
      done.returncode == 1 and "cwd" in done.stderr, str(done)[:200])
# A malformed payload stops the hook before it can gate, exactly as a rejected config does, and a
# stopped Stop hook is non-blocking - so this too must leave the durable record, not only a
# traceback that scrolls away. This log is cumulative across the isolated env's earlier calls (the
# no-transcript_path one above wrote to it too), so `cwd`, unique to this run, identifies its line.
gate_log = open(isolated["MEMENTO_CEILING_LOG"]).read()
check("and the malformed payload that stopped the gate is recorded in the log, not only stderr",
      "-> stopped" in gate_log and "cwd" in gate_log, gate_log)

# stdin that parses but is not an object has no session to gate. It must stop loudly, and the stop
# must still be recorded - the log names a session from a dict, so a non-dict payload reaching the
# log call unguarded would raise inside the handler and skip the very line it exists to write.
for raw in ("42", "null", "[]", "not json at all"):
    env = dict(isolated, MEMENTO_CEILING_LOG=os.path.join(scratch_dir(), "log"))
    done = subprocess.run([sys.executable, HOOK], input=raw, text=True, capture_output=True, env=env)
    recorded = open(env["MEMENTO_CEILING_LOG"]).read()
    check(f"a stdin payload {raw!r} that is not a Stop object fails loudly and is still recorded",
          done.returncode == 1 and "-> stopped" in recorded, f"{done.returncode} | {recorded!r}")

# A plugin root can contain a space (~/Library/Application Support/...), and unquoted the
# only exit from the block fails to execute. The shared config module is copied in beside the
# hook because a plugin root is the whole directory: the hook resolves `lib/` relative to
# itself, so a root holding the script alone is not a root this hook can run from.
spaced_root = os.path.join(scratch_dir(), "ceiling test")
spaced_hook = os.path.join(spaced_root, "hooks", "scripts", os.path.basename(HOOK))
os.makedirs(os.path.dirname(spaced_hook))
shutil.copy(HOOK, spaced_hook)
shutil.copytree(LIB, os.path.join(spaced_root, "lib"))
spaced_launcher = os.path.join(spaced_root, "skills", "message-in-a-bottle",
                               "bin", "finalize-session")
try:
    _, out, _ = run([user, assistant(OVER)], hook=spaced_hook)
    check("a launcher path containing a space is quoted in the instruction",
          out and f"'{spaced_launcher}'" in out["reason"], str(out)[:200])
finally:
    shutil.rmtree(os.path.dirname(spaced_root))

check("the launcher the hook points at exists", os.access(LAUNCHER, os.X_OK), LAUNCHER)
# [LAW:one-source-of-truth] RESET_MARKER is a fact about the launcher's rendered output living
# in the hook. The hook cannot be imported - it reads stdin at module level - so the pattern is
# lifted from its source and run here against the real thing and against the texts that merely
# describe it. Renamed in the launcher and nowhere else, every close-out would go uncredited and
# every session would be stuck at a block it cannot satisfy, silently.
marker = re.compile(re.search(r'^RESET_MARKER = re\.compile\(r"(.*)"\)$',
                              open(HOOK).read(), re.M).group(1))
check("the marker the hook credits is what a real close-out prints",
      bool(marker.search(SCHEDULED)), f"{marker.pattern!r} vs {SCHEDULED!r}")
# What the pattern keys on has to survive in the launcher, or it stops matching real output and
# nothing here notices - the source never matched it and never will.
scheduling = [line for line in open(LAUNCHER) if re.search(r'^\s*echo "handoff scheduled', line)]
check("the launcher still prints the pieces the marker reads",
      len(scheduling) == 3 and all("handoff scheduled →" in line
                                   and " in ${HANDOFF_DELAY_SECONDS}s " in line
                                   and "(log: $LOGFILE)" in line for line in scheduling),
      str(scheduling))
# The two texts that defeated the bare words. Both say `handoff scheduled`; neither renders the
# delay or the log path, which is the whole reason the marker is a rendered line and not a phrase.
check("the launcher's own source does not match the marker",
      not any(marker.search(line) for line in scheduling), str(scheduling))
prose = [line for line in open(CONTRACT) if "handoff scheduled" in line]
check("the close-out contract's prose does not match the marker",
      len(prose) >= 2 and not any(marker.search(line) for line in prose), str(prose))
registered = json.load(open(os.path.join(os.path.dirname(HERE), "hooks.json")))["hooks"]
# The count is only true of the live context at a stop: a session reset in place keeps its
# transcript, so on any earlier event the newest record can describe a context already gone. The
# one other event registered measures nothing: it drops a session override on /clear.
check("the gate is registered on Stop, and the override reset on SessionStart, and nothing else",
      sorted(registered) == ["SessionStart", "Stop"], str(sorted(registered)))
command = registered["Stop"][0]["hooks"][0]["command"]
check("the Stop registration runs this script, from the plugin root",
      os.path.basename(HOOK) in command and "${CLAUDE_PLUGIN_ROOT}" in command, command)
check("the hook is executable", os.access(HOOK, os.X_OK), HOOK)
# A compaction is also a SessionStart, with source "compact", and the context after it is the same
# session still running - its override must stand. The matcher is what keeps the hook off it.
starting = registered["SessionStart"]
check("the /clear hook is matched to source clear alone",
      len(starting) == 1 and starting[0].get("matcher") == "clear", str(starting))
check("and runs the clear script, from the plugin root",
      os.path.basename(CLEAR_HOOK) in starting[0]["hooks"][0]["command"]
      and "${CLAUDE_PLUGIN_ROOT}" in starting[0]["hooks"][0]["command"], str(starting))
check("the /clear hook is executable", os.access(CLEAR_HOOK, os.X_OK), CLEAR_HOOK)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
