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
LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        "skills", "message-in-a-bottle", "bin", "finalize-session")
# The skill the block instruction tells the agent to load before running the close-out. It
# describes the launcher's report, so its prose reads like that report without being one.
CONTRACT = os.path.join(os.path.dirname(os.path.dirname(LAUNCHER)), "SKILL.md")
# [LAW:one-source-of-truth] every case drives the threshold explicitly, so the shipped
# default lives in the hook alone and retuning it cannot break these. A fixture magnitude,
# not a second copy of that number.
TEST_CEILING = 100_000
USER_CEILING = f"ceiling = {TEST_CEILING}\n"
OVER, UNDER = TEST_CEILING + 20_000, TEST_CEILING - 60_000
SESSION = "s-1"
CONFIG_NAME = "memento.conf"
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
SCHEDULED = "handoff scheduled \u2192 tmux memento:1.0 (/compact) in 10s (log: /tmp/l)"
RECORDED = ("handoff recorded \u2192 /tmp/m.md\n"
            "no reset: this session keeps its context, so carry on with the work.")


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
# --reset is the whole coupling to finalize-session's contract: a handoff without it resets
# nothing, so the instruction that omitted it would ask for a close-out that never closes out.
check("the reason hands over the flag that actually resets the session",
      out and "--reset compact" in out["reason"], str(out))
# A worktree-isolated session may be refused the command above by the platform, which deleting
# this hook's own gate did nothing to change.
check("the reason tells a worktree session how to reach a directory it can run from",
      out and "ExitWorktree" in out["reason"], str(out))

code, out, _ = run([user, assistant(OVER)], stop_hook_active=True)
check("a stop already blocked once is not blocked again",
      code == 0 and out and "decision" not in out, f"{code} {out}")
check("giving up is loud rather than silent",
      out and "context ceiling breached" in out.get("systemMessage", ""), str(out))
check("giving up does not claim the close-out failed",
      out and "If the close-out did not run" in out.get("systemMessage", "")
      and "was NOT closed out" not in out.get("systemMessage", ""), str(out))

ran_closeout = [user, assistant(OVER),
                tool_use("Bash", {"command": f"{LAUNCHER} --reset compact 'bye'"}),
                tool_result(content=SCHEDULED)]
code, out, _ = run(ran_closeout)
check("a stop right after the close-out ran is allowed",
      code == 0 and out and "decision" not in out
      and "the close-out ran" in out.get("systemMessage", ""), str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} --reset compact 'bye'"}),
                    tool_result(is_error=True, content=SCHEDULED)])
check("a close-out that was refused does not count as having run",
      out and out.get("decision") == "block", str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": "git status"}), tool_result()])
check("a call that is not the close-out does not count as one",
      out and out.get("decision") == "block", str(out))
# Without --reset the launcher records the handoff and the session carries on with its context
# untouched, and it says so. Crediting that would let a session at the ceiling satisfy this
# check every turn and never free a token - the failure the block exists to prevent.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}),
                    tool_result(content=RECORDED)])
check("a launcher call that reset nothing is not credited as the close-out",
      out and out.get("decision") == "block", str(out))
# The instruction hands out an absolute path, but an agent holding the skill may reach the
# launcher by name, or through a wrapper. What it was called does not decide this; what it did.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": "finalize-session --reset compact 'bye'"}),
                    tool_result(content=SCHEDULED)])
check("a bare-name close-out is credited at the stop",
      code == 0 and out and "decision" not in out
      and "the close-out ran" in out.get("systemMessage", ""), str(out))
# The two ways a command string lied. Naming the launcher and the flag is not running them:
# the first of these ran nothing at all, and the second is a handoff message quoting the flag,
# which finalize-session parses as no flag and this hook once read as one.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command":
                        "echo 'reminder: run finalize-session --reset compact before stopping'"}),
                    tool_result(content="reminder: run finalize-session --reset compact")])
check("a command that only mentions the close-out is not credited as running it",
      out and out.get("decision") == "block", str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command":
                        f"{LAUNCHER} 'Next agent: run --reset compact once the audit is done'"}),
                    tool_result(content=RECORDED)])
check("--reset quoted inside the handoff message is not a close-out",
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
# finalize-session's own contract allows backticks/$ in a message, which the deleted parser
# choked on; a real, successful close-out written that way must still be credited.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command":
                        f'{LAUNCHER} --reset compact "See `git rev-parse HEAD`"'}),
                    tool_result(content=SCHEDULED)])
check("a close-out containing shell-special characters is credited if it ran",
      code == 0 and out and "decision" not in out
      and "the close-out ran" in out.get("systemMessage", ""), str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} --reset compact 'bye'"}, call_id="a"),
                    tool_result("a", content=SCHEDULED),
                    tool_use("Bash", {"command": "git push"}, call_id="b"),
                    tool_result("b")])
check("a confirming git call after the close-out does not undo it",
      code == 0 and out and "decision" not in out
      and "the close-out ran" in out.get("systemMessage", ""), str(out))
# The tmux transport compacts in place, so the transcript keeps growing past a close-out.
# Crediting that one forever would wave through every later breach in the same file.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} --reset compact 'bye'"}),
                    tool_result(content=SCHEDULED),
                    user, assistant(OVER)])
check("a close-out from an earlier turn does not excuse a later breach",
      out and out.get("decision") == "block", str(out))

# --- the ceiling is configurable while sessions run --------------------------------------

def blocked_at(out, ceiling):
    return bool(out) and out.get("decision") == "block" and f"{ceiling:,}" in out.get("reason", "")


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
_, out, _ = run([user, assistant(400_000)], user_conf="ceiling = +30000\n",
                project_conf="ceiling = +20000\n")
check("adjustments at two layers both apply, in order", blocked_at(out, 300_000), str(out))
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
      blocked_at(out, 260_000), str(out))

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
code, out, err = run([user, assistant(OVER)], user_conf="ceiling = 10000\n",
                     session_conf="ceiling = -50000\n")
check("adjustments that resolve below zero fail loudly, naming both layers",
      code == 1 and "never negative" in err and err.count(CONFIG_NAME) == 2, f"{code} {err}")

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
                    tool_use("Bash", {"command": f"{LAUNCHER} --reset compact 'bye'"}, sidechain=True),
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
run([user, assistant(OVER)])
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
# The count is only true at a stop, so being called anywhere else means hooks.json has drifted
# from this file. Measuring anyway is how a session gets denied on a number that is not its own.
code, out, err = run([user, assistant(OVER)], event="PreToolUse")
check("an event that is not Stop stops the hook rather than measuring",
      code == 1 and "PreToolUse" in err and "hooks.json" in err, f"{code} {err[:200]}")
done = subprocess.run([sys.executable, HOOK], input='{"hook_event_name": "Stop"}', text=True,
                      capture_output=True, env=isolated)
check("a payload with no transcript_path fails loudly",
      done.returncode == 1 and "transcript_path" in done.stderr, str(done)[:200])
# The project config is resolved from it, so a payload without it is a hook that would
# silently read no project config at all.
empty_transcript = write_conf(os.path.join(scratch_dir(), "t.jsonl"), "")
done = subprocess.run([sys.executable, HOOK], text=True, capture_output=True, env=isolated,
                      input=json.dumps({"hook_event_name": "Stop", "session_id": SESSION,
                                        "transcript_path": empty_transcript}))
check("a payload with no cwd fails loudly",
      done.returncode == 1 and "cwd" in done.stderr, str(done)[:200])

# A plugin root can contain a space (~/Library/Application Support/...), and unquoted the
# only exit from the block fails to execute.
spaced_root = os.path.join(scratch_dir(), "ceiling test")
spaced_hook = os.path.join(spaced_root, "hooks", "scripts", os.path.basename(HOOK))
os.makedirs(os.path.dirname(spaced_hook))
shutil.copy(HOOK, spaced_hook)
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
# The no-reset path must not carry it, or a recorded handoff would credit a reset that never
# happened - the failure that made the launcher's report worth reading in the first place.
recording = [line for line in open(LAUNCHER) if re.search(r'^\s*echo "handoff recorded', line)]
check("the launcher's record-only report does not match the marker",
      len(recording) == 1 and not marker.search(recording[0]), str(recording))
registered = json.load(open(os.path.join(os.path.dirname(HERE), "hooks.json")))["hooks"]
# The count is only true of the live context at a stop: a session reset in place keeps its
# transcript, so on any earlier event the newest record can describe a context already gone.
check("the hook is registered on Stop alone",
      sorted(registered) == ["Stop"], str(sorted(registered)))
command = registered["Stop"][0]["hooks"][0]["command"]
check("the Stop registration runs this script, from the plugin root",
      os.path.basename(HOOK) in command and "${CLAUDE_PLUGIN_ROOT}" in command, command)
check("the hook is executable", os.access(HOOK, os.X_OK), HOOK)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
