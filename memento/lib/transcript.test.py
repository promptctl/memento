#!/usr/bin/env python3
"""Tests for the transcript reader: which records the relaunch settings come from, and every way a
transcript can fail to record them. The reader runs in process against transcripts written under a
scratch config directory, and the script form runs once as the launcher runs it.

Run with: python3 transcript.test.py
"""

import json
import os
import subprocess
import sys
import tempfile

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import hermetic  # noqa: E402

hermetic.home()
import transcript  # noqa: E402
from transcript import Settings, Unrecorded, recorded_settings  # noqa: E402

SESSION = "5f0c2b1e-8d3a-4c6f-9e7b-1a2d3c4e5f60"
failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def prompt(mode):
    return {"type": "user", "permissionMode": mode, "message": {"role": "user", "content": "go"}}


def state(mode):
    return {"type": "permission-mode", "permissionMode": mode, "sessionId": SESSION}


def answer(effort, model="claude-opus-5-5"):
    """An assistant record; effort=None writes one with no effort field, as a model with no effort
    level, an older claude, or a `<synthetic>` record does."""
    record = {"type": "assistant", "message": {"role": "assistant", "model": model, "content": []}}
    if effort is not None:
        record["effort"] = effort
    return record


def tool_result():
    return {"type": "user", "toolUseResult": {}, "message": {"role": "user", "content": []}}


def config(*transcripts, session=SESSION, tail=""):
    """A config directory holding one transcript per entry, each a list of records, in its own project
    slug. `tail` is appended raw to the last one, as a partial append leaves it."""
    root = tempfile.mkdtemp()
    for slug, records in enumerate(transcripts):
        project = os.path.join(root, "projects", f"-project-{slug}")
        os.makedirs(project)
        with open(os.path.join(project, session + ".jsonl"), "w") as handle:
            handle.writelines(json.dumps(record) + "\n" for record in records)
            if slug == len(transcripts) - 1:
                handle.write(tail)
    return root


def refusal(root, session=SESSION):
    try:
        return f"no refusal: {recorded_settings(root, session)}"
    except Unrecorded as error:
        return str(error)


# --- which record wins ---------------------------------------------------------

root = config([prompt("default"), answer("medium"), prompt("bypassPermissions"), answer("high"),
               tool_result()])
check("the newest mode and effort are read, past a newer tool result",
      recorded_settings(root, SESSION) == Settings("bypassPermissions", "high"),
      recorded_settings(root, SESSION))

# The session-state batch is appended after the last prompt, so its mode is the newer one.
root = config([prompt("default"), answer("high"), state("acceptEdits")])
check("a permission-mode record newer than the last prompt wins",
      recorded_settings(root, SESSION).mode == "acceptEdits", recorded_settings(root, SESSION))
root = config([state("acceptEdits"), prompt("default"), answer("high")])
check("a prompt newer than the permission-mode record wins",
      recorded_settings(root, SESSION).mode == "default", recorded_settings(root, SESSION))

# A subagent's records are its own session's, not this one's.
root = config([prompt("plan"), answer("low"),
               {**prompt("bypassPermissions"), "isSidechain": True},
               {**answer("max"), "isSidechain": True}])
check("sidechain records are not the session's",
      recorded_settings(root, SESSION) == Settings("plan", "low"), recorded_settings(root, SESSION))

root = config([prompt("auto"), answer("xhigh")], tail='{"type": "assistant", "effort": "lo')
check("a partial last line is skipped",
      recorded_settings(root, SESSION) == Settings("auto", "xhigh"), recorded_settings(root, SESSION))

# The walk reads backward in chunks, so a record that straddles a chunk boundary is read whole.
padding = [{"type": "attachment", "pad": "x" * 1000} for _ in range(400)]
root = config([prompt("dontAsk"), answer("high")] + padding)
check("records older than one chunk are reached",
      os.path.getsize(transcript.session_transcript(root, SESSION)) > transcript.TAIL_CHUNK
      and recorded_settings(root, SESSION) == Settings("dontAsk", "high"),
      recorded_settings(root, SESSION))

for mode in sorted(transcript.PERMISSION_MODES):
    root = config([prompt(mode), answer("high")])
    check(f"every mode claude accepts is read: {mode}",
          recorded_settings(root, SESSION).mode == mode, recorded_settings(root, SESSION))

# --- every way the transcript fails to record them -------------------------------

root = config([prompt("default"), answer("high")], session="another-session")
check("no transcript for the session is refused, naming the count",
      refusal(root).startswith("0 transcripts for session " + SESSION), refusal(root))

root = config([prompt("default"), answer("high")], [prompt("plan"), answer("high")])
check("two transcripts for the session are refused, naming both",
      refusal(root).startswith("2 transcripts") and refusal(root).count(".jsonl") == 2, refusal(root))

root = config([answer("high"), tool_result()])
check("no record carrying a mode is refused",
      "carries permissionMode" in refusal(root), refusal(root))

# --- effort is optional in the domain ---------------------------------------------

root = config([prompt("default")])
check("a session with no answer yet records no effort, and that is not a refusal",
      recorded_settings(root, SESSION) == Settings("default", None), refusal(root))

root = config([prompt("plan"), answer(None, model="claude-haiku-4-5-20251001")])
check("a model with no effort level records none, and the settings say so",
      recorded_settings(root, SESSION) == Settings("plan", None), refusal(root))

root = config([prompt("plan"), {**answer(None), "effort": None}])
check("an effort recorded as null is no effort, not an unknown level",
      recorded_settings(root, SESSION) == Settings("plan", None), refusal(root))

# A `<synthetic>` record is not an answer from the session's model, so it does not erase the effort.
root = config([prompt("plan"), answer("high"), answer(None, model="<synthetic>")])
check("a synthetic record newer than a real answer leaves the real answer's effort",
      recorded_settings(root, SESSION) == Settings("plan", "high"), refusal(root))

# After /model to a model with no effort level, the earlier model's effort is not the session's.
root = config([prompt("plan"), answer("high"), answer(None, model="claude-haiku-4-5-20251001")])
check("a newer real answer with no effort means no effort, not the older answer's",
      recorded_settings(root, SESSION) == Settings("plan", None), refusal(root))

check("the relaunch flags carry the effort when there is one",
      transcript.relaunch_flags(Settings("auto", "max")) == "--permission-mode auto --effort max")
check("and no --effort at all when there is none",
      transcript.relaunch_flags(Settings("auto", None)) == "--permission-mode auto")

for bad in ("yolo", "", None, {"mode": "plan"}):
    root = config([prompt(bad), answer("high")])
    check(f"a mode claude does not accept is refused, not passed on: {bad!r}",
          "records permissionMode" in refusal(root), refusal(root))
for bad in ("ludicrous", "", {"level": "high"}):
    root = config([prompt("plan"), answer(bad)])
    check(f"an effort claude does not accept is refused: {bad!r}",
          "records effort" in refusal(root), refusal(root))

# A glob character in the config directory or the session id is a literal, never a pattern.
root = config([prompt("plan"), answer("high")], [prompt("plan"), answer("high")])
check("the session id is matched literally, not as a pattern",
      refusal(root, session="*").startswith("0 transcripts"), refusal(root, session="*"))

# --- whether a relaunch took its handoff --------------------------------------

NEEDLE = "This handoff verbatim on disk: /handoffs/20261110T031731Z-1.md"


def handoff(content, **fields):
    return {"type": "user", "message": {"role": "user", "content": content}, **fields}


took = transcript.took_prompt
root = config([handoff("=== Below is a message ===\nnext\n\n" + NEEDLE)])
check("a prompt holding the handoff's path is taken", took(root, SESSION, NEEDLE))
root = config([handoff([{"type": "text", "text": "before"}, {"type": "text", "text": NEEDLE}])])
check("so is one whose content is text blocks", took(root, SESSION, NEEDLE))
root = config([handoff(NEEDLE, isMeta=True)])
check("a meta record holding the path is not the prompt", not took(root, SESSION, NEEDLE))
root = config([handoff(NEEDLE, isSidechain=True)])
check("nor is a subagent's record", not took(root, SESSION, NEEDLE))
root = config([handoff("some other prompt"), answer("high")])
check("a session that took another prompt has not taken this one", not took(root, SESSION, NEEDLE))
root = config([tool_result()])
check("a tool result is no prompt", not took(root, SESSION, NEEDLE))
root = config()
check("a session that has written nothing yet has not taken it", not took(root, SESSION, NEEDLE))
root = config([handoff(NEEDLE)], [handoff(NEEDLE)])
try:
    took(root, SESSION, NEEDLE)
    check("two transcripts for the id are a reason, not an answer", False, "no raise")
except Unrecorded as error:
    check("two transcripts for the id are a reason, not an answer",
          str(error).startswith("2 transcripts"), error)

# --- the script form, as the launcher runs it ------------------------------------

SCRIPT = os.path.join(LIB, "transcript.py")


def script(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)


root = config([prompt("bypassPermissions"), answer("high")])
done = script("flags", root, SESSION)
check("flags prints the relaunch flags on one line and exits 0",
      done.returncode == 0 and done.stdout == "--permission-mode bypassPermissions --effort high\n"
      and done.stderr == "", done)
done = script("flags", root, "absent")
check("flags refuses on stderr with exit 2 and prints nothing on stdout",
      done.returncode == 2 and done.stdout == "" and "0 transcripts" in done.stderr, done)
root = config([handoff(NEEDLE)])
done = script("prompted", root, SESSION, NEEDLE)
check("prompted exits 0 when the prompt was taken", done.returncode == 0 and done.stderr == "", done)
done = script("prompted", root, SESSION, "another handoff")
check("and 1 when it was not, saying nothing", done.returncode == 1 and done.stderr == "", done)
root = config([handoff(NEEDLE)], [handoff(NEEDLE)])
done = script("prompted", root, SESSION, NEEDLE)
check("and 2 with the reason when it cannot tell",
      done.returncode == 2 and "2 transcripts" in done.stderr, done)
# Python's own exit on an uncaught exception is 1, the status that means not yet.
root = config([{"type": "user", "message": "a message that is a string"}])
done = script("prompted", root, SESSION, NEEDLE)
check("a reader that crashes on a record says so with 2 and the traceback, never 1",
      done.returncode == 2 and "Traceback" in done.stderr, done)
for args in ((root, SESSION), ("flags", root), ("prompted", root, SESSION), ("bogus", root, SESSION)):
    done = script(*args)
    check(f"the script refuses a malformed call with exit 2: {args[0] if args[0] != root else '(none)'}",
          done.returncode == 2 and "usage:" in done.stderr, done)

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
