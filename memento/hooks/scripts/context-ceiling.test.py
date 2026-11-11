#!/usr/bin/env python3
"""Unit tests for the context-ceiling hook.

Each case feeds a synthetic transcript and the payload Claude Code delivers to the hook as a
subprocess, then asserts the JSON it emits. Run with `python3 context-ceiling.test.py`.
"""

import atexit
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

# One scratch root for every mkdtemp() below, removed on exit - each call still gets its own
# subdirectory, but the suite no longer abandons one per case in the system temp dir.
SCRATCH = tempfile.mkdtemp(prefix="context-ceiling-test-")
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
# The hooks install memento's cc-candybar plugin under XDG_DATA_HOME; every child this suite runs
# inherits a scratch one, so no run reaches the plugin root of the machine it runs on.
os.environ["XDG_DATA_HOME"] = os.path.join(SCRATCH, "data")


def scratch_dir():
    return tempfile.mkdtemp(dir=SCRATCH)


HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "context-ceiling.py")
PLUGIN = os.path.dirname(os.path.dirname(HERE))
LIB = os.path.join(PLUGIN, "lib")
LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        "skills", "message-in-a-bottle", "bin", "finalize-session")
JOURNAL = os.path.join(PLUGIN, "skills", "journal", "bin", "journal")
# Every case passes its threshold explicitly, so the shipped default lives only in ceiling_config
# and retuning it cannot break these. The fixture magnitude is not a second copy of that number.
TEST_CEILING = 100_000
USER_CEILING = f"ceiling = {TEST_CEILING}\n"
OVER, UNDER = TEST_CEILING + 20_000, TEST_CEILING - 60_000
SESSION = "s-1"
# The file names come from the module the hook itself reads them from, so a fixture here cannot
# drift from the files the hook looks for. [LAW:one-source-of-truth]
sys.path.insert(0, LIB)
from ceiling_config import CONFIG_NAME, DEFAULT_CEILING, GRACE  # noqa: E402

CLEAR_HOOK = os.path.join(HERE, "clear-session-ceiling.py")
STATUS_HOOK = os.path.join(HERE, "session-status.py")

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
SCHEDULED = "handoff scheduled → tmux memento:1.0 in 10s (log: /tmp/l)"


def tool_result(call_id="t-1", is_error=False, content="out", sidechain=False):
    block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "isSidechain": sidechain, "message": {"content": [block]}}


user = {"type": "user", "isSidechain": False, "message": {"content": "hi"}}


def run(records, event="Stop", stop_hook_active=False,
        hook=HOOK, user_conf=USER_CEILING, project_conf=None, session_conf=None,
        project=None, config_home=None, xdg=None, extra_env=None,
        session=SESSION):
    """Invoke the hook as Claude Code does and return (exit code, parsed stdout, stderr). Every
    config layer is rooted in scratch space and ambient ones (CLAUDE_PROJECT_DIR, XDG_CONFIG_HOME)
    are stripped, so a developer's own config cannot decide a test. user_conf=None writes no user
    layer, so the case reaches the shipped default."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    handle.write("".join(json.dumps(r) + "\n" for r in records))
    handle.close()
    log = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
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
    os.unlink(log.name)
    return done.returncode, out, done.stderr



# --- Stop -------------------------------------------------------------------------------

code, out, _ = run([user, assistant(UNDER)])
check("under the ceiling, the stop is allowed", code == 0 and out is None, f"{code} {out}")

code, out, _ = run([user, assistant(OVER)])
check("over the ceiling, the stop is blocked", out and out.get("decision") == "block", f"{code} {out}")

# --- the grace: a unit in progress is finished before the close-out ---------------------------

# Between the ceiling and the limit the stop is still blocked once, because the agent has to be told;
# what it is told is to finish the unit it is in and close out after, not to drop it where it stands.
code, out, _ = run([user, assistant(OVER)])

# A finishing block restarts the turn, and the stop that ends that turn is marked as following a
# block. Past the limit, that stop is the one the limit exists for. The case below builds the block
# from the hook's own output, as the harness would write it.
_, finishing, _ = run([user, assistant(OVER)])
_, closing, _ = run([user, assistant(PAST_LIMIT)])


def fed_back(verdict):
    return {"type": "user", "isSidechain": False,
            "message": {"role": "user", "content": f"Stop hook feedback:\n{verdict['reason']}"}}


code, out, _ = run([user, assistant(OVER), fed_back(finishing), assistant(OVER + 10_000)],
                   stop_hook_active=True)
check("while under the limit that turn's stop is let through",
      code == 0 and out and "decision" not in out, str(out))
code, out, _ = run([user, assistant(PAST_LIMIT), fed_back(closing), assistant(PAST_LIMIT + 5_000)],
                   stop_hook_active=True)
check("a closing block is never followed by a second one",
      code == 0 and out and "decision" not in out, str(out))
code, out, _ = run([user, assistant(OVER), fed_back(finishing), assistant(PAST_LIMIT),
                    fed_back(closing), assistant(PAST_LIMIT + 5_000)], stop_hook_active=True)
check("and the escalation happens once, not at every stop past the limit",
      code == 0 and out and "decision" not in out, str(out))
# The limit rides on the ceiling rather than being set beside it, so a ceiling switched off has none.
code, out, _ = run([user, assistant(5_000_000)], user_conf="ceiling = off\n")
check("a ceiling switched off has no limit either", code == 0 and out is None, f"{code} {out}")

ran_closeout = [user, assistant(OVER),
                tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}),
                tool_result(content=SCHEDULED)]
code, out, _ = run(ran_closeout)
check("a stop right after the close-out ran is allowed",
      code == 0 and out and "decision" not in out, str(out))
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
      code == 0 and out and "decision" not in out, str(out))
# A command string that names the launcher without running it.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command":
                        "echo 'reminder: run finalize-session before stopping'"}),
                    tool_result(content="reminder: run finalize-session")])
check("a command that only mentions the close-out is not credited as running it",
      out and out.get("decision") == "block", str(out))
# Bash output is written by the agent's own command, so a rendered line inside a command proves
# nothing. A case that echoes the line must not be credited.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"echo '{SCHEDULED}'"}),
                    tool_result(content=SCHEDULED)])
check("a command that echoes the launcher's line is not the launcher",
      out and out.get("decision") == "block", str(out))
# A real invocation cannot carry the report it produces, because the log path is made while it runs.
# So the line's presence in the command separates printing it from causing it.
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
      code == 0 and out and "decision" not in out, str(out))
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}, call_id="a"),
                    tool_result("a", content=SCHEDULED),
                    tool_use("Bash", {"command": "git push"}, call_id="b"),
                    tool_result("b")])
check("a confirming git call after the close-out does not undo it",
      code == 0 and out and "decision" not in out, str(out))
# The tmux transport clears in place, so the transcript keeps growing past a close-out.
# Crediting that one forever would wave through every later breach in the same file.
code, out, _ = run([user, assistant(OVER),
                    tool_use("Bash", {"command": f"{LAUNCHER} 'bye'"}),
                    tool_result(content=SCHEDULED),
                    user, assistant(OVER)])
check("a close-out from an earlier turn does not excuse a later breach",
      out and out.get("decision") == "block", str(out))

# --- the ceiling is configurable while sessions run --------------------------------------

code, out, _ = run([user, assistant(5_000_000)], user_conf="ceiling = off\n",
                   session_conf="ceiling = +10000\n")
check("adjusting a ceiling that is switched off leaves it off", code == 0 and out is None, f"{code} {out}")
for word in ("off", "OFF"):
    code, out, _ = run([user, assistant(5_000_000)], user_conf=f"ceiling = {word}\n")
    check(f"the gate can be switched off by writing {word!r}",
          code == 0 and out is None, f"{code} {out}")

# The project is the session's, not wherever a Bash call last left the shell.
elsewhere = scratch_dir()
code, out, _ = run([user, assistant(60_000)], project_conf="ceiling = 50000\n",
                   extra_env={"CLAUDE_PROJECT_DIR": elsewhere})
check("a cwd config is not read when CLAUDE_PROJECT_DIR points somewhere else",
      code == 0 and out is None, f"{code} {out}")

# --- the ceiling is a live read of the layers at every stop ---------------------------------

# Nothing is frozen per session, so a shared layer edited, deleted or created while a session runs
# governs its next stop. Each case therefore runs one session more than once against one config home.
# The effect at a stop is a block, since Stop is the only event this runs on.

home = scratch_dir()
code, out, _ = run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
check("a session's first stop passes under the shared ceiling as it stands",
      code == 0 and out is None, f"{code} {out}")
code, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 450000\n")
check("a shared ceiling raised under a running session lets its next stop through",
      code == 0 and out is None, f"{code} {out}")


# Between stops the hook writes nothing but the log: the sessions tree holds only what `ceiling set
# session` puts there, so there is no per-session record to go stale.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, user_conf="ceiling = 350000\n")
run([user, assistant(400_000)], config_home=home, user_conf="ceiling = 350000\n")
check("a session that only stops leaves nothing under the sessions tree",
      not os.path.exists(os.path.join(home, "sessions")),
      str(os.path.exists(os.path.join(home, "sessions")) and os.listdir(os.path.join(home, "sessions"))))

# The session's own layer moves the ceiling immediately, in both directions - which is what makes the
# block escapable from inside the block.
home = scratch_dir()
code, out, _ = run([user, assistant(300_000)], config_home=home, user_conf="ceiling = 250000\n",
                   session_conf="ceiling = +100_000\n")
check("a session layer written mid-session raises the ceiling on the very next stop",
      code == 0 and out is None, f"{code} {out}")


# --- a session override resets with the session, /clear included ------------------------------

# A kill-and-relaunch reset gets a new session id and drops its override. `/clear` keeps the id, so
# the SessionStart hook matched to it removes the override file.

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
code, err = cleared(home)
check("a /clear in a session that set no override is not an error", code == 0 and not err, f"{code} {err}")

# A drifted hooks.json is the way this hook would fire on the wrong event and drop an override the
# session still means to have. Any payload that is not a /clear stops it.
home = scratch_dir()
run([user, assistant(1_000)], config_home=home, session_conf="ceiling = 500000\n")
for event, source in (("SessionStart", "compact"), ("SessionStart", "startup"), ("Stop", "clear")):
    code, err = cleared(home, event=event, source=source)
    check(f"a {event} payload with source {source!r} stops the hook rather than dropping the override",
          code == 1 and os.path.exists(os.path.join(home, "sessions", SESSION, CONFIG_NAME)), f"{code} {err}")
# A real Stop payload carries no `source` at all; the refusal has to be the curated one, not a
# KeyError raised while composing it.
code, err = cleared(home, raw=json.dumps({"session_id": SESSION, "hook_event_name": "Stop"}))
check("a real Stop payload, which has no source, is refused",
      code == 1, f"{code} {err}")
for raw in ("42", "[]", "not json", "{}"):
    code, err = cleared(home, raw=raw)
    check(f"a /clear payload of {raw!r} stops the hook",
          code == 1, f"{code} {err}")
code, err = cleared(home, session="../..")
check("a session id that is not a bare name is refused by the /clear hook too",
      code == 1, f"{code} {err}")

# --- a setting nobody can misspell into silence -------------------------------------------

code, out, err = run([user, assistant(OVER)], user_conf="ceiling = 350k\n")
check("a ceiling that does not parse fails loudly",
      code == 1, f"{code} {err}")
code, out, err = run([user, assistant(OVER)], user_conf="ceilling = 350000\n")
check("a misspelled key fails loudly rather than reading as a setting nobody made",
      code == 1, f"{code} {err}")
# Bytes that are not text must fail loudly, because a traceback from a Stop hook leaves the gate off
# with nothing stating why.
bytes_home = scratch_dir()
with open(os.path.join(bytes_home, CONFIG_NAME), "wb") as handle:
    handle.write(b"ceiling = 35\xff0000\n")
code, out, err = run([user, assistant(400_000)], config_home=bytes_home, user_conf=None)
check("a shared file of bytes that are not text fails loudly",
      code == 1, f"{code} {err}")
# The key this setting used to be spelled with is a rejection, not a synonym. Every other
# case here writes the current spelling, so nothing else in the suite would notice an alias
# readmitted for compatibility.
code, out, err = run([user, assistant(OVER)], user_conf="context_ceiling = 350000\n")
check("the retired key is refused rather than read as a synonym",
      code == 1, f"{code} {err}")
code, out, err = run([user, assistant(OVER)], user_conf="ceiling 350000\n")
check("a line with no `=` fails loudly",
      code == 1, f"{code} {err}")
code, out, err = run([user, assistant(OVER)], user_conf="ceiling =\n")
check("a key written with no value fails loudly, unlike an exported-empty variable",
      code == 1, f"{code} {err}")
code, out, err = run([user, assistant(OVER)],
                     user_conf="ceiling = 300000\nceiling = 400000\n")
check("one key set twice in one file fails loudly",
      code == 1, f"{code} {err}")
# The shared fold is the value written down, so it must not resolve negative. A recorded negative
# read back as an adjustment resolves to a positive value nobody set, which passes the check below.
# This case asserts the fold is rejected at the recording stop as well as at later ones.
code, out, err = run([user, assistant(OVER)],
                     user_conf=f"ceiling = -{DEFAULT_CEILING + 50_000}\n")
check("a shared fold that resolves below zero fails loudly rather than being recorded",
      code == 1, f"{code} {err}")

code, out, err = run([user, assistant(OVER)], user_conf="ceiling = 10000\n",
                     session_conf="ceiling = -50000\n")
check("adjustments that resolve below zero fail loudly",
      code == 1, f"{code} {err}")

# The disabling word is matched on the text as written, not after digit separators are removed.
# `o_f_f` is a typo and must not switch the gate off.
code, out, err = run([user, assistant(5_000_000)], user_conf="ceiling = o_f_f\n")
check("'o_f_f' is a typo rather than a way to switch the gate off",
      code == 1, f"{code} {err}")

# `str.isdigit` was true of these and `int` was not, so the crafted message was skipped and a
# traceback took its place. The shape the parse accepts and the one it converts are now one.
for exotic in ("²", "⁵"):
    code, out, err = run([user, assistant(OVER)],
                         user_conf=f"ceiling = {exotic}\n")
    check(f"a digit-like character {exotic!r} that is not a number fails loudly",
          code == 1, f"{code} {err}")

# Underscores group digits as Python's own literals do, so what a person writes in a config and
# what they would write in code are the same set - no more, and no less.
for malformed in ("_350000", "350000_", "3__50000", "+_100", "1_"):
    code, out, err = run([user, assistant(OVER)],
                         user_conf=f"ceiling = {malformed}\n")
    check(f"{malformed!r} is not a number of tokens", code == 1,
          f"{code} {err}")

# The session id becomes a path in one place, so its shape is checked there. An absolute id would
# discard the sessions directory, and a relative one could climb out of it.
for escape in ("/tmp", "../..", "a/b", "..", "./..", "..//", ".", "/"):
    code, out, err = run([user, assistant(OVER)], session=escape,
                         user_conf="ceiling = 300000\n")
    check(f"a session id of {escape!r} is refused rather than read as a directory",
          code == 1, f"{code} {err}")

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

# --- wiring --------------------------------------------------------------------------------

isolated = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
isolated["MEMENTO_CONFIG_HOME"] = scratch_dir()
isolated["MEMENTO_CEILING_LOG"] = os.path.join(scratch_dir(), "log")
done = subprocess.run([sys.executable, HOOK], input="{}", text=True, capture_output=True,
                      env=isolated)
check("a payload with no event fails loudly", done.returncode == 1, str(done)[:200])
# The count is only true at a stop, so being called anywhere else means hooks.json has drifted
# from this file. Measuring anyway is how a session gets denied on a number that is not its own.
code, out, err = run([user, assistant(OVER)], event="PreToolUse")
check("an event that is not Stop stops the hook rather than measuring",
      code == 1, f"{code} {err[:200]}")
done = subprocess.run([sys.executable, HOOK], input='{"hook_event_name": "Stop"}', text=True,
                      capture_output=True, env=isolated)
check("a payload with no transcript_path fails loudly",
      done.returncode == 1, str(done)[:200])
# The project config is resolved from it, so a payload without it is a hook that would
# silently read no project config at all.
empty_transcript = write_conf(os.path.join(scratch_dir(), "t.jsonl"), "")
done = subprocess.run([sys.executable, HOOK], text=True, capture_output=True, env=isolated,
                      input=json.dumps({"hook_event_name": "Stop", "session_id": SESSION,
                                        "transcript_path": empty_transcript}))
check("a payload with no cwd fails loudly",
      done.returncode == 1, str(done)[:200])

# stdin that parses but is not an object has no session to gate. It must stop loudly.
for raw in ("42", "null", "[]", "not json at all"):
    env = dict(isolated, MEMENTO_CEILING_LOG=os.path.join(scratch_dir(), "log"))
    done = subprocess.run([sys.executable, HOOK], input=raw, text=True, capture_output=True, env=env)
    check(f"a stdin payload {raw!r} that is not a Stop object fails loudly",
          done.returncode == 1, f"{done.returncode}")

# A block gives the two commands of a close-out on lines of their own, and an agent runs them as
# given: the journal's, which records the summary, and then the launcher's.
for what, verdict in (("finishing", finishing), ("closing", closing)):
    given = [shlex.split(line)[0] for line in verdict["reason"].splitlines()
             if line.startswith("    ")]
    check(f"a {what} block gives the journal's command and then the launcher's, and both exist",
          given == [JOURNAL, LAUNCHER] and all(os.access(named, os.X_OK) for named in given),
          str(given))
check("the hook is executable", os.access(HOOK, os.X_OK), HOOK)
check("every SessionStart script is executable",
      all(os.access(script, os.X_OK) for script in (CLEAR_HOOK, STATUS_HOOK)))
check("the /clear hook is executable", os.access(CLEAR_HOOK, os.X_OK), CLEAR_HOOK)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
