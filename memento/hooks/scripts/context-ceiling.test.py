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
import time

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
from ceiling_config import (CONFIG_NAME, DEFAULT_CEILING, GRACE,  # noqa: E402
                            SHARED_AT_START, lines_in)

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

# --- a running session keeps the ceiling it started under ----------------------------------

# The incident this section exists for. A shared file held 350,000 from 05:18 on 2026-09-06 until
# an agent working in an unrelated project removed the line at 14:55. Every running session read
# the default from the next tool call on; one of them was at 250,196 tokens, went from
# unrestricted to fully gated between two calls, and never wrote a handoff. Each case runs a
# session more than once against one config home, because a second run seeing what the first one
# recorded is the whole of what is under test.

home = scratch_dir()
code, out, _ = run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
check("a session's first stop passes under the shared ceiling it started with",
      code == 0 and out is None, f"{code} {out}")
code, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 250000\n")
check("a shared ceiling lowered under a running session does not gate it",
      code == 0 and out is None, f"{code} {out}")
_, out, _ = run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 250000\n")
check("nor does it move that session's ceiling", blocked_at(out, 350_000), str(out))
# The other half of the rule: the rewrite is not ignored, it is scoped. Same config home, so the
# only difference between this case and the one above it is which session is stopping.
_, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 250000\n",
                session="s-after")
check("a session started after the rewrite does get the new number",
      blocked_at(out, 250_000), str(out))

# What actually happened in the incident was a deletion, and a file that no longer exists has no
# mtime to rank against the session's start - so this is the case that decides the design, not a
# variation on the one above.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
os.unlink(os.path.join(home, CONFIG_NAME))
_, out, _ = run([user, assistant(400_000)], config_home=home, user_conf=None)
check("a shared ceiling deleted under a running session leaves that session's ceiling standing",
      blocked_at(out, 350_000), str(out))
# A shared file rewritten into something `ceiling_in` exits on is the same class of change, and
# the hook exiting is how the gate stops running for a session entirely.
code, out, err = run([user, assistant(400_000)], config_home=home, user_conf="ceilling = 350000\n")
check("a shared file broken after a session started does not take that session's gate down",
      code == 0 and blocked_at(out, 350_000), f"{code} {err}")
code, out, err = run([user, assistant(400_000)], config_home=home,
                     user_conf="ceilling = 350000\n", session="s-into-breakage")
check("while a session starting into that broken file still fails loudly",
      code == 1 and "ceilling" in err, f"{code} {err}")
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
code, out, err = run([user, assistant(400_000)], config_home=bytes_home, user_conf=None,
                     session="s-into-bytes")
check("a shared file of bytes that are not text fails in the parser's own voice",
      code == 1 and "not text" in err and "Traceback" not in err, f"{code} {err}")

# The rule is about shared layers, so the project layer is frozen on the same terms as the user
# one - it is shared with every other session anchored at that project.
home, repo = scratch_dir(), scratch_dir()
run([user, assistant(1_000)], config_home=home, project=repo, project_conf="ceiling = 350000\n")
_, out, _ = run([user, assistant(400_000)], config_home=home, project=repo,
                project_conf="ceiling = 250000\n")
check("a project ceiling rewritten under a running session is frozen out too",
      blocked_at(out, 350_000), str(out))

# STOP CONDITION 2: the session's own layer is the one that still moves, immediately, in both
# directions - which is what makes the block escapable from inside the block.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 250000\n")
code, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 250000\n",
                   session_conf="ceiling = +100_000\n")
check("a session layer written mid-session raises the ceiling on the very next stop",
      code == 0 and out is None, f"{code} {out}")
_, out, _ = run([user, assistant(200_000)], config_home=home, user_conf="ceiling = 250000\n",
                session_conf="ceiling = -100_000\n")
check("and lowers it on the next stop too - no direction test, no asymmetry",
      blocked_at(out, 150_000), str(out))
# The ceiling skill hands a session back by deleting this file, which must leave the record it
# sits beside untouched. The live shared file is moved to 200,000 so a re-read would show.
os.unlink(os.path.join(home, "sessions", SESSION, CONFIG_NAME))
_, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 200000\n")
check("deleting the session layer hands the session back to its recorded start, not the live file",
      blocked_at(out, 250_000), str(out))

# A record half-written by a session killed mid-write would exist and parse to nothing, and a
# record that can neither be read nor replaced is the gate off for that session for good. Written
# whole and moved into place, so the empty file below is a state only this test can build.
home = scratch_dir()
write_conf(os.path.join(home, "sessions", SESSION, SHARED_AT_START), "")
_, out, _ = run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 350000\n")
check("a record left empty by an interrupted write is completed rather than stopping the gate",
      blocked_at(out, 350_000), str(out))
_, out, _ = run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 250000\n")
check("and the completed record is what the next stop reads", blocked_at(out, 350_000), str(out))

# `off` has to survive the round trip through the record, or a session that started with the gate
# off would silently get it back the moment the shared file moved.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = off\n")
code, out, _ = run([user, assistant(5_000_000)], config_home=home, user_conf="ceiling = 250000\n")
check("a session that started with the gate off keeps it off when the shared file turns it back on",
      code == 0 and out is None, f"{code} {out}")
_, out, _ = run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 250000\n",
                session_conf="ceiling = 300000\n")
check("and its own layer can still put a number back", blocked_at(out, 300_000), str(out))

# --- a session reset in place re-freezes only when the reset lands -------------------------

# The tmux transport resets a session in place, so it keeps its id and its record. Without a refresh
# it would hold its first context's shared ceiling forever; refreshing unconditionally would re-freeze
# a session whose reset never landed from files that moved under it. The close-out marks the size the
# context had; the next stop below it is the fresh context and re-derives, one above it is the same
# context still running and keeps what it froze. Same session and config home across runs, because a
# second run seeing what the first recorded is the whole of what is under test.

closeout = [tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}), tool_result(content=SCHEDULED)]

# The reset landed: the context after the close-out is smaller than it was at the close-out, so the
# next context picks up the shared change made while the first one ran.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
run([user, assistant(400_000)] + closeout, config_home=home, user_conf="ceiling = 350000\n")
_, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 250000\n")
check("a session that closed out and carried on picks up a shared change made while it ran",
      blocked_at(out, 250_000), str(out))
# And the re-derived record is itself frozen: the refresh happens once, at the landing, not at every
# later stop, so a further shared change does not move the new context either.
_, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 150000\n")
check("the re-derived record is frozen in turn - a later shared change does not move it again",
      blocked_at(out, 250_000), str(out))

# The reset never landed: the context after the close-out is no smaller (the scheduled reset failed,
# or the session carried on before it fired), so the original ceiling stands rather than being
# re-frozen from a shared file that has since moved.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
run([user, assistant(400_000)] + closeout, config_home=home, user_conf="ceiling = 350000\n")
_, out, _ = run([user, assistant(450_000)], config_home=home, user_conf="ceiling = 250000\n")
check("a session whose reset never landed is not re-frozen from a shared file that moved under it",
      blocked_at(out, 350_000), str(out))

# The common path: a close-out at the end of a unit of work happens under the ceiling, not only when
# the ceiling forces one, so the marker is recorded on an allowed stop too and the next context still
# re-freezes from the current shared layers.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
code, out, _ = run([user, assistant(300_000)] + closeout, config_home=home, user_conf="ceiling = 350000\n")
check("a close-out under the ceiling is an allowed stop", code == 0 and out is None, f"{code} {out}")
_, out, _ = run([user, assistant(280_000)], config_home=home, user_conf="ceiling = 250000\n")
check("a close-out under the ceiling still re-freezes the next context from the current shared layers",
      blocked_at(out, 250_000), str(out))

# Repeated close-outs before any reset lands: each records the size the context had, so the marker
# tracks the latest, and the conservative test still waits for the context to fall below it. A context
# that only grows between close-outs never reads as a landed reset and keeps what it froze.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
run([user, assistant(380_000)] + closeout, config_home=home, user_conf="ceiling = 350000\n")
_, out, _ = run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 250000\n")
check("a context still growing after a close-out is not read as a landed reset",
      blocked_at(out, 350_000), str(out))
run([user, assistant(420_000)] + closeout, config_home=home, user_conf="ceiling = 250000\n")
_, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 250000\n")
check("a reset landing below the latest close-out size is what finally re-derives",
      blocked_at(out, 250_000), str(out))

# --- a setting nobody can misspell into silence -------------------------------------------

code, out, err = run([user, assistant(OVER)], user_conf="ceiling = 350k\n")
check("a ceiling that does not parse fails loudly, naming the file and the line",
      code == 1 and "350k" in err and "line 1" in err and CONFIG_NAME in err, f"{code} {err}")
code, out, err = run([user, assistant(OVER)], user_conf="ceilling = 350000\n")
check("a misspelled key fails loudly rather than reading as a setting nobody made",
      code == 1 and "ceilling" in err and "ceiling" in err, f"{code} {err}")
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
check("and it names the shared file that caused it, not the record derived from it",
      code == 1 and CONFIG_NAME in err and SHARED_AT_START not in err, f"{code} {err}")

code, out, err = run([user, assistant(OVER)], user_conf="ceiling = 10000\n",
                     session_conf="ceiling = -50000\n")
# Both layers by name, not by a count of filenames: the shared side of the fold now reaches the
# message through the session's recorded start, so the two sources are two different files.
check("adjustments that resolve below zero fail loudly, naming both layers",
      code == 1 and "never negative" in err and SHARED_AT_START in err and CONFIG_NAME in err,
      f"{code} {err}")

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
# transcript, so on any earlier event the newest record can describe a context already gone.
check("the hook is registered on Stop alone",
      sorted(registered) == ["Stop"], str(sorted(registered)))
command = registered["Stop"][0]["hooks"][0]["command"]
check("the Stop registration runs this script, from the plugin root",
      os.path.basename(HOOK) in command and "${CLAUDE_PLUGIN_ROOT}" in command, command)
check("the hook is executable", os.access(HOOK, os.X_OK), HOOK)

# --- the sessions tree does not grow without bound ----------------------------------------
# Driven through the hook the way the harness drives it: a stop writes and ages records; the sweep and
# the touch that keep the tree bounded are read off the filesystem afterwards, not off the internals -
# with one exception at the end, a direct lines_in call for a race too fine to trigger through the hook
# deterministically. [LAW:behavior-not-structure]

def aged_session(home, sid, age_days, extra=()):
    """A session directory as it would stand `age_days` after it was last seen: its record, any extra
    files, and the directory itself all stamped that far in the past. The directory's own mtime is set
    last, because writing a file into it bumps that mtime back to now."""
    directory = os.path.join(home, "sessions", sid)
    os.makedirs(directory, exist_ok=True)
    when = time.time() - age_days * 86_400
    for name, text in ((SHARED_AT_START, f"ceiling = {DEFAULT_CEILING}\n"), *extra):
        stamped = write_conf(os.path.join(directory, name), text)
        os.utime(stamped, (when, when))
    os.utime(directory, (when, when))
    return directory


def survives(home, sid):
    return os.path.exists(os.path.join(home, "sessions", sid, SHARED_AT_START))


# A session unseen well past the cutoff is finished; its whole directory goes, and the stray `.<pid>`
# partial a killed record write would have orphaned inside it goes with it.
swept = scratch_dir()
aged_session(swept, "ancient", 40, extra=[(f"{SHARED_AT_START}.9999", "ceiling = 1\n")])
run([user, assistant(UNDER)], config_home=swept, session="fresh-1")
check("a session unseen past the cutoff is swept, partial and all",
      not os.path.exists(os.path.join(swept, "sessions", "ancient")),
      os.listdir(os.path.join(swept, "sessions")))
check("the session doing the sweeping does not sweep its own fresh record",
      survives(swept, "fresh-1"), os.listdir(os.path.join(swept, "sessions")))

# A session seen within the cutoff is still in play - a pane resumed days later is still that session
# - so it is left exactly where it is.
kept = scratch_dir()
aged_session(kept, "recent", 0)
run([user, assistant(UNDER)], config_home=kept, session="fresh-2")
check("a session seen within the cutoff is left alone",
      survives(kept, "recent"), os.listdir(os.path.join(kept, "sessions")))

# The stop condition itself: a session that keeps stopping cannot be swept, however long ago it
# started. The record starts 40 days old; the session stops once, which touches it back to now; a
# brand-new session then runs the sweep, and the touched record is what saves the running one. Remove
# the touch and this is the case that deletes a live session's record.
running = scratch_dir()
aged_session(running, "old-runner", 40)
run([user, assistant(UNDER)], config_home=running, session="old-runner")
run([user, assistant(UNDER)], config_home=running, session="fresh-3")
check("a session that keeps stopping is never swept, however long ago it started",
      survives(running, "old-runner"), os.listdir(os.path.join(running, "sessions")))

# A fresh `.<pid>` partial is a record or override write still in flight, not litter: the directory is
# kept, because reaping it out from under the write would make that write fail. (An old partial, like
# the one aged with the "ancient" case above, ages out with everything else.)
inflight = scratch_dir()
mid = aged_session(inflight, "mid-write", 40)
write_conf(os.path.join(mid, f"{SHARED_AT_START}.9999"), "ceiling = 1\n")  # a write in flight, just now
run([user, assistant(UNDER)], config_home=inflight, session="fresh-4")
check("a fresh partial (a write in flight) keeps its directory from being swept",
      survives(inflight, "mid-write"), os.listdir(os.path.join(inflight, "sessions")))

# A per-session ceiling the user set recently keeps its whole directory alive even when the record is
# old: _last_seen reads the session's own layer too, so a deliberate override is never swept out from
# under a session that set it, stopped or not.
override = scratch_dir()
kept_dir = aged_session(override, "set-override", 40)
write_conf(os.path.join(kept_dir, CONFIG_NAME), "ceiling = +100000\n")  # set just now
run([user, assistant(UNDER)], config_home=override, session="fresh-5")
check("a freshly-set per-session override keeps its directory from being swept",
      survives(override, "set-override"), os.listdir(os.path.join(override, "sessions")))


# A file that vanishes between lines_in's exists() check and its read - a concurrent sweep deleting a
# session's files while its own hook reads them - reads as absent, not a crash that would take the
# reader's Stop gate down. lines_in only calls .exists() and .read_text(), so a stand-in exercises the
# race deterministically.
class _VanishedMidRead:
    def exists(self):
        return True

    def read_text(self, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")


check("lines_in reads a file that vanished mid-read as absent rather than crashing",
      lines_in(_VanishedMidRead()) == [], "expected []")

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
