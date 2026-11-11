#!/usr/bin/env python3
"""Tests for the journal-wake hook: which prompts journal a wake, and that none is held back.

Each case feeds the hook the payload Claude Code delivers at UserPromptSubmit, as a subprocess,
against a journal and a log of its own. The handoff in them is one the real launcher wrote, so the
lines the launcher frames a handoff with and the lines this hook knows one by are held to each
other here. Run with: python3 journal-wake.test.py
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="journal-wake-test-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "journal-wake.py")
PLUGIN = os.path.dirname(os.path.dirname(HERE))
LAUNCHER = os.path.join(PLUGIN, "skills", "message-in-a-bottle", "bin", "finalize-session")
sys.path.insert(0, os.path.join(PLUGIN, "lib"))
from journal import Wake, entries  # noqa: E402

# The session that wrote the handoff, which the handoff names, and the one it wakes.
WRITER, WAKER = "session-that-closed-out", "session-that-woke"

failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def scratch_dir():
    return Path(tempfile.mkdtemp(dir=SCRATCH))


def launched(message):
    """A handoff the real launcher made, as its file's path and its text. The launcher writes the
    handoff before it looks for the claude it runs under, so a dry run with no $CLAUDE_PID is
    refused and leaves the handoff it wrote."""
    home = scratch_dir()
    done = subprocess.run(
        [LAUNCHER, message], text=True, capture_output=True, timeout=30,
        env={"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(home),
             "MEMENTO_HANDOFF_DIR": str(home / "handoffs"), "FINALIZE_DRY_RUN": "1",
             "MEMENTO_JOURNAL": str(home / "journal.jsonl"), "CLAUDE_CODE_SESSION_ID": WRITER})
    written = sorted((home / "handoffs").glob("*"))
    if len(written) != 1:
        sys.exit(f"the launcher left {len(written)} handoffs where these tests need the one it "
                 f"writes: rc={done.returncode} out={done.stdout!r} err={done.stderr!r}")
    return str(written[0]), written[0].read_text()


def pasted(text):
    """A prompt as Claude Code 2.1.299 delivers a paste of this text."""
    return f'\n\n<pasted_content id="df66">\n{text}\n</pasted_content id="df66">\n'


def run(prompt, journal, event="UserPromptSubmit", cwd=SCRATCH):
    """Runs the hook on a prompt as Claude Code does. Returns the completed process and the lines
    it logged. The environment names another session than the payload does, as a hook's may."""
    log = scratch_dir() / "hook.log"
    env = dict(os.environ, MEMENTO_JOURNAL=str(journal), MEMENTO_CEILING_LOG=str(log),
               CLAUDE_CODE_SESSION_ID="session-of-the-environment")
    payload = {"session_id": WAKER, "hook_event_name": event, "cwd": str(cwd), "prompt": prompt}
    done = subprocess.run([sys.executable, HOOK], text=True, capture_output=True, env=env,
                          input=json.dumps(payload))
    return done, log.read_text().splitlines() if log.exists() else []


def passed_on(done):
    """Whether a run left its prompt alone. Claude Code adds this hook's stdout to the session's
    context, and takes exit 2 as a refusal of the prompt."""
    return done.stdout == "" and done.returncode != 2


repo = SCRATCH / "repo"
subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/journal-lane"], cwd=repo, check=True,
               capture_output=True)

path, handoff = launched("Branch fix/parser-null, PR #91. Resume at step 10.")

# --- a prompt that is a handoff journals one wake ------------------------------------------------
# The wake holds the path as the handoff's last line gives it, character for character: a wake
# finds its close-out by that text, and the launcher's close-out holds the same.

for how, prompt in (("as a session's initial prompt", handoff),
                    ("pasted, as Claude Code wraps a paste", pasted(handoff)),
                    ("with blank lines around it", f"\n{handoff}\n\n")):
    journal = scratch_dir() / "journal.jsonl"
    done, logged = run(prompt, journal, cwd=repo)
    kept = entries(journal)
    check(f"a launcher-made handoff {how} journals one wake naming its file, for the session and "
          f"the directory the payload names",
          done.returncode == 0 and len(kept) == 1 and isinstance(kept[0], Wake)
          and (kept[0].session, kept[0].cwd, kept[0].branch, kept[0].handoff)
          == (WAKER, str(repo), "journal-lane", path), (done, kept))
    check(f"and the prompt is passed on, with the wake in the log ({how})",
          passed_on(done) and len(logged) == 1 and logged[0].endswith(f"-> woke from {path}"),
          (done, logged))

# --- any other prompt journals nothing -----------------------------------------------------------
# A handoff is the whole of its prompt. One that is quoted is something the session was shown, not
# what it woke from, wherever in the prompt the quote stands.

for what, prompt in (
        ("an ordinary prompt", "what did the last session do?"),
        ("a handoff quoted in the middle of other text", f"Look at this:\n{handoff}\nWhat woke it?"),
        ("a handoff quoted after other text", f"Look at this:\n{handoff}"),
        ("a handoff quoted before other text", f"{handoff}\nWhat woke it?"),
        ("a paste that quotes a handoff in the middle of other text",
         pasted(f"Look at this:\n{handoff}\nWhat woke it?")),
        ("a pasted handoff with words typed before it", "Look at this: " + pasted(handoff))):
    journal = scratch_dir() / "journal.jsonl"
    done, logged = run(prompt, journal)
    check(f"{what} journals nothing and is passed on, logged as no handoff",
          done.returncode == 0 and passed_on(done) and entries(journal) == []
          and len(logged) == 1 and logged[0].endswith("-> not a handoff"), (done, logged))

# --- a wake that cannot be recorded fails where it shows, and holds nothing back --------------------
# Each of these is a hook error Claude Code shows the user, with the prompt delivered as it was.

relative = handoff.replace(path, "handoffs/20261110T031731Z-1.md")
journal = scratch_dir() / "journal.jsonl"
done, logged = run(relative, journal)
check("a handoff whose last line names a path that is not absolute journals nothing, with the "
      "journal's reason on stderr and in the log",
      relative != handoff and done.returncode == 1 and passed_on(done) and entries(journal) == []
      and "is not absolute" in done.stderr
      and len(logged) == 1 and "-> stopped: " in logged[0] and "is not absolute" in logged[0],
      (done, logged))

blocked = scratch_dir() / "a-file"
blocked.write_text("in the journal's way\n")
done, logged = run(handoff, blocked / "journal.jsonl")
check("a handoff whose wake the journal cannot take fails without holding the prompt back",
      done.returncode == 1 and passed_on(done) and blocked.read_text() == "in the journal's way\n"
      and len(logged) == 1 and "-> stopped: " in logged[0], (done, logged))

journal = scratch_dir() / "journal.jsonl"
done, logged = run(handoff, journal, event="Stop")
check("a payload for another event journals nothing and names the event it was registered on",
      done.returncode == 1 and passed_on(done) and entries(journal) == []
      and "registered on UserPromptSubmit" in done.stderr
      and len(logged) == 1 and "-> stopped: " in logged[0], (done, logged))

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
