#!/usr/bin/env python3
"""Unit tests for the `ceiling` command.

Each case runs the real command as a subprocess against a scratch config home and a scratch git
repo, then asserts what the files say and what the command printed. [LAW:behavior-not-structure]
it is driven the way an agent drives it, so a rewrite of the internals cannot break these.

The cases that matter most run the real Stop hook afterwards and compare its resolved ceiling to
the one this command reported. That agreement is the contract - two programs, one number - and it
is the only thing that proves a ceiling this command wrote is a ceiling the gate enforces.

Run: python3 ceiling.test.py
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRATCH = tempfile.mkdtemp(prefix="ceiling-test-")
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "ceiling")
PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
HOOK = os.path.join(PLUGIN, "hooks", "scripts", "context-ceiling.py")
# The file-format facts come from the module the command and the hook both read them from, so a
# fixture here cannot drift from the files production writes. [LAW:one-source-of-truth]
sys.path.insert(0, os.path.join(PLUGIN, "lib"))
from ceiling_config import (CONFIG_NAME, DEFAULT_CEILING,  # noqa: E402
                            PROJECT_CONFIG_DIR, SHARED_AT_START)

SESSION = "sess-1"
failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def scratch_dir():
    return tempfile.mkdtemp(dir=SCRATCH)


def world(user_conf=None, project_conf=None, session_conf=None, recorded=None, git=True):
    """A config home and a repo with a subdirectory, each layer written where a person writes it.

    Returns (home, repo). Layers are driven through the real files rather than through flags, so
    these cases exercise the paths the command actually resolves. `recorded` writes the frozen
    shared record the hook keeps, which is what makes a session one that has already stopped."""
    home, repo = scratch_dir(), scratch_dir()
    os.makedirs(os.path.join(repo, "sub"))
    if git:
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
    for path, text in ((os.path.join(home, CONFIG_NAME), user_conf),
                       (os.path.join(repo, PROJECT_CONFIG_DIR, CONFIG_NAME), project_conf),
                       (os.path.join(home, "sessions", SESSION, CONFIG_NAME), session_conf),
                       (os.path.join(home, "sessions", SESSION, SHARED_AT_START), recorded)):
        if text is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(text)
    return home, repo


def run(home, cwd, *argv, session=SESSION, anchor=None, pythonpath=None):
    """Invoke the command as the skill invokes it. Every ambient config is stripped: a developer's
    own ceiling or CLAUDE_PROJECT_DIR must not decide a test.

    `anchor` sets CLAUDE_PROJECT_DIR, for the cases where the session's project and the directory
    the process happens to stand in are deliberately not the same place."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "XDG_CONFIG_HOME", "CLAUDE_CODE_SESSION_ID")}
    env["MEMENTO_CONFIG_HOME"] = home
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    if anchor is not None:
        env["CLAUDE_PROJECT_DIR"] = anchor
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    done = subprocess.run([sys.executable, CLI, *argv], text=True, capture_output=True,
                          env=env, cwd=cwd)
    return done.returncode, done.stdout, done.stderr


def aged_argparse():
    """A directory that, placed on a child's PYTHONPATH, makes its argparse behave as every
    argparse before 3.13 did.

    3.13 widened the regex argparse uses to tell a negative number from an unrecognised option.
    This interpreter is past that, so a value like `-50_000` parses here whether or not the command
    does anything to protect it, and a case asserting that it works would pass against a command
    that had no guard at all. Restoring the old matcher is what makes the guard testable rather
    than merely present. [LAW:verifiable-goals]"""
    directory = scratch_dir()
    with open(os.path.join(directory, "sitecustomize.py"), "w") as handle:
        handle.write("import argparse, re\n"
                     "_born = argparse._ActionsContainer.__init__\n"
                     "def aged(self, *a, **kw):\n"
                     "    _born(self, *a, **kw)\n"
                     r"    self._negative_number_matcher = re.compile(r'^-\d+$|^-\d*\.\d+$')"
                     "\nargparse._ActionsContainer.__init__ = aged\n")
    return directory


def gate(home, repo, tokens, session=SESSION):
    """The real Stop hook, at a token count, against the same layers. Returns (verdict, ceiling)
    read from its log line - the hook's own statement of which number won."""
    transcript = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=SCRATCH)
    transcript.write(json.dumps({"type": "user", "isSidechain": False,
                                 "message": {"content": "hi"}}) + "\n")
    transcript.write(json.dumps({"type": "assistant", "isSidechain": False, "message": {"usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": tokens - 2, "output_tokens": 0}}}) + "\n")
    transcript.close()
    log = os.path.join(scratch_dir(), "ceiling.log")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "XDG_CONFIG_HOME")}
    env.update({"MEMENTO_CONFIG_HOME": home, "MEMENTO_CEILING_LOG": log})
    done = subprocess.run([sys.executable, HOOK], text=True, capture_output=True, env=env,
                          cwd=repo, input=json.dumps(
                              {"session_id": session, "hook_event_name": "Stop", "cwd": repo,
                               "transcript_path": transcript.name, "stop_hook_active": False}))
    if done.returncode != 0:
        return f"exit {done.returncode}: {done.stderr.strip()}", None
    emitted = json.loads(done.stdout) if done.stdout.strip() else None
    line = open(log).read().strip().split("\n")[-1]
    verdict = "block" if emitted and emitted.get("decision") == "block" else "allow"
    # Read as a float, because a lifted ceiling is logged as `inf` - which is the hook agreeing
    # with the command about a session that has no ceiling at all, not a malformed line.
    return verdict, float(line.split("ceiling=")[1].split()[0])


def conf(*parts):
    path = os.path.join(*parts)
    return open(path).read() if os.path.exists(path) else None


def project_conf(repo):
    return conf(repo, PROJECT_CONFIG_DIR, CONFIG_NAME)


def session_conf(home):
    return conf(home, "sessions", SESSION, CONFIG_NAME)


# --- show -------------------------------------------------------------------------------

home, repo = world()
code, out, err = run(home, repo, "show")
check("with nothing set, the shipped default is what is in force",
      code == 0 and f"{DEFAULT_CEILING:,} tokens" in out, f"{code} {out} {err}")
check("and show says so for this session and for a new one alike",
      out.count(f"{DEFAULT_CEILING:,} tokens") == 2, out)
check("show lists the layers that are absent rather than omitting them",
      all(label in out for label in ("shared at start", "session layer", "project layer",
                                     "user layer")) and out.count("(unset)") == 2, out)
check("show writes nothing", project_conf(repo) is None and session_conf(home) is None,
      f"{project_conf(repo)} {session_conf(home)}")

code, out, err = run(home, repo, "show", session=None)
check("without a session id the command refuses rather than guessing",
      code == 1 and "CLAUDE_CODE_SESSION_ID" in err, f"{code} {err}")

code, out, err = run(home, repo, "show", session="../escape")
check("a session id that is not a bare name is refused",
      code == 1 and "session directory" in err, f"{code} {err}")

code, out, err = run(home, repo)
check("no command at all is an invocation error, not a default action",
      code == 2, f"{code} {out} {err}")

code, out, err = run(home, repo, "set", "everywhere", "off")
check("a scope the command does not have is refused by the parser",
      code == 2 and "everywhere" in err, f"{code} {err}")

# --- set session ------------------------------------------------------------------------

home, repo = world()
code, out, err = run(home, repo, "set", "session", "+100_000")
check("a signed move raises this session by that much",
      code == 0 and "350,000 tokens" in out, f"{code} {out} {err}")
check("the session layer holds the resolved number, which is what the hook reads back",
      session_conf(home) == "ceiling = 350000\n", session_conf(home))
check("a session-scoped move leaves the project alone", project_conf(repo) is None,
      str(project_conf(repo)))
# The number a new session here would get is the one a session-scoped move must NOT touch.
check("and show still reports the default for a new session here",
      f"a new session here  {DEFAULT_CEILING:,} tokens" in out, out)

# A second move starts from the first, because `+50_000` means more room than I have now.
code, out, err = run(home, repo, "set", "session", "+50_000")
check("a second signed move starts from the ceiling now in force",
      code == 0 and "400,000 tokens" in out and session_conf(home) == "ceiling = 400000\n",
      f"{out} {session_conf(home)}")

code, out, err = run(home, repo, "set", "session", "300_000")
check("an absolute move replaces what stood rather than adding to it",
      code == 0 and session_conf(home) == "ceiling = 300000\n", session_conf(home))

code, out, err = run(home, repo, "set", "session", "off")
check("off lifts the ceiling for this session",
      code == 0 and "no ceiling" in out and session_conf(home) == "ceiling = off\n",
      f"{out} {session_conf(home)}")

code, out, err = run(home, repo, "set", "session", "+100_000")
check("a signed move on top of no ceiling is still no ceiling",
      code == 0 and session_conf(home) == "ceiling = off\n", session_conf(home))

home, repo = world()
code, out, err = run(home, repo, "set", "session", "-500_000")
check("a move resolving below zero fails loudly",
      code == 1 and "never negative" in err, f"{code} {err}")
check("and writes nothing on the way out", session_conf(home) is None, str(session_conf(home)))

code, out, err = run(home, repo, "set", "session", "100k")
check("a unit suffix is refused, naming what the setting accepts",
      code == 1 and "100k" in err and "off" in err, f"{code} {err}")
check("and writes nothing on the way out", session_conf(home) is None, str(session_conf(home)))

# The documented `-50_000` against an argparse that calls a leading `-` an option unless the token
# is plain digits. Every Python before 3.13 is that argparse, and there it took the value for an
# option, never reached the grammar, and exited 2 on a usage error.
home, repo = world()
code, out, err = run(home, repo, "set", "session", "-50_000", pythonpath=aged_argparse())
check("a negative adjustment is a value and not an option on every interpreter",
      code == 0 and session_conf(home) == "ceiling = 200000\n", f"{code} {out} {err}")
code, out, err = run(home, repo, "set", "session", pythonpath=aged_argparse())
check("and a value left off there is still an invocation error rather than a silent default",
      code == 2, f"{code} {err}")

# --- set project ------------------------------------------------------------------------

home, repo = world()
code, out, err = run(home, repo, "set", "project", "+100_000")
check("a project-scoped move writes the project layer",
      code == 0 and project_conf(repo) == "ceiling = 350000\n", str(project_conf(repo)))
check("and this session's layer as well, because the shared layers were frozen for it",
      session_conf(home) == "ceiling = 350000\n", str(session_conf(home)))
# The invariant the whole design turns on: one number, stated by both files.
check("so this session and a new session here run under the same ceiling",
      "this session        350,000 tokens" in out and "a new session here  350,000 tokens" in out,
      out)
check("the command names every file it wrote", out.count("wrote ") == 2, out)

# A subdirectory is where the command runs, not where the project is.
home, repo = world()
code, out, err = run(home, os.path.join(repo, "sub"), "set", "project", "400_000")
check("run from a subdirectory, the project ceiling lands at the repository root",
      code == 0 and project_conf(repo) == "ceiling = 400000\n", str(project_conf(repo)))
check("and no second file appears in the subdirectory",
      conf(repo, "sub", PROJECT_CONFIG_DIR, CONFIG_NAME) is None,
      str(conf(repo, "sub", PROJECT_CONFIG_DIR, CONFIG_NAME)))

# An existing file is the one in force, so it is the one rewritten - a fold, never a stack.
home, repo = world(project_conf="# this repo runs long\nceiling = 350_000\n")
code, out, err = run(home, repo, "set", "project", "+100_000")
check("headroom added to a project that already set a ceiling folds into its one line",
      code == 0 and project_conf(repo) == "ceiling = 450000\n", str(project_conf(repo)))
check("and the result is a file the reader still accepts",
      code == 0 and "450,000 tokens" in out, f"{code} {out}")

# A file deeper than the root is the one in force for this directory, so it is the one written.
home, repo = world(project_conf=None)
deeper = os.path.join(repo, "sub", PROJECT_CONFIG_DIR, CONFIG_NAME)
os.makedirs(os.path.dirname(deeper))
open(deeper, "w").write("ceiling = 300_000\n")
code, out, err = run(home, os.path.join(repo, "sub"), "set", "project", "+50_000")
check("the project file already in force here is the one rewritten",
      code == 0 and conf(repo, "sub", PROJECT_CONFIG_DIR, CONFIG_NAME) == "ceiling = 350000\n",
      str(conf(repo, "sub", PROJECT_CONFIG_DIR, CONFIG_NAME)))
check("and no shadowing file is created at the root above it", project_conf(repo) is None,
      str(project_conf(repo)))

home, repo = world(user_conf="ceiling = 500_000\n")
code, out, err = run(home, repo, "set", "project", "+100_000")
check("a project move starts from the user layer where that is what stands beneath it",
      code == 0 and project_conf(repo) == "ceiling = 600000\n", str(project_conf(repo)))

home, repo = world(git=False)
code, out, err = run(home, repo, "set", "project", "+100_000")
check("outside a git repository a project move refuses rather than guessing a root",
      code == 1 and "not inside a git repository" in err, f"{code} {err}")
check("and writes nothing, including the session layer",
      project_conf(repo) is None and session_conf(home) is None,
      f"{project_conf(repo)} {session_conf(home)}")

# The project is the session's project, not whichever directory something last ran `cd` into.
# Every other case here runs with the anchor and the working directory in the same place, which is
# precisely where a lookup that asked the working directory could not be seen to be asking it.
home, repo = world()
elsewhere = scratch_dir()
subprocess.run(["git", "init", "-q", elsewhere], check=True, capture_output=True)
code, out, err = run(home, elsewhere, "set", "project", "400_000", anchor=repo)
check("a project move writes the anchored project even when run from another repository",
      code == 0 and project_conf(repo) == "ceiling = 400000\n",
      f"{code} {project_conf(repo)} {err}")
check("and leaves the repository it happened to be standing in alone",
      conf(elsewhere, PROJECT_CONFIG_DIR, CONFIG_NAME) is None,
      str(conf(elsewhere, PROJECT_CONFIG_DIR, CONFIG_NAME)))

home, repo = world()
code, out, err = run(home, scratch_dir(), "set", "project", "400_000", anchor=repo)
check("and finds the anchored repository from a directory that is no repository at all",
      code == 0 and project_conf(repo) == "ceiling = 400000\n", f"{code} {err}")

# What a move replaces is printed as it goes, the way `clear` prints what it removes. A project
# move writes this session's layer too, so it is the one that can overwrite a number nobody else
# knows about.
home, repo = world(session_conf="ceiling = 900_000\n")
code, out, err = run(home, repo, "set", "project", "400_000")
check("a move says what each file held, so a session's own ceiling is not lost silently",
      code == 0 and "replacing 900_000" in out, out)

# A session that has already stopped holds a frozen record; the project move must reach past it.
home, repo = world(recorded=f"ceiling = {DEFAULT_CEILING}\n")
code, out, err = run(home, repo, "set", "project", "+100_000")
check("a session whose shared layers are already frozen still gets the project's new ceiling",
      code == 0 and "this session        350,000 tokens" in out, f"{code} {out}")

# A session that started with no ceiling at all still takes the project's number, because the
# session layer this writes is absolute and an absolute ignores the record beneath it.
home, repo = world(recorded="ceiling = off\n")
code, out, err = run(home, repo, "set", "project", "400_000")
check("a session that started with no ceiling is brought under the project's new one",
      code == 0 and "this session        400,000 tokens" in out, f"{code} {out}")
verdict, enforced = gate(home, repo, 450_000)
check("and the gate enforces it against a session that was previously ungated",
      verdict == "block" and enforced == 400_000, f"{verdict} {enforced}")

# A project move does not inherit one session's private allowance.
home, repo = world(session_conf="ceiling = 900_000\n")
code, out, err = run(home, repo, "set", "project", "+100_000")
check("a project move starts from the project, not from the room one session gave itself",
      code == 0 and project_conf(repo) == "ceiling = 350000\n", str(project_conf(repo)))

# --- one project, and no further ---------------------------------------------------------

# The requirement this command was built for: a change reaches the project it was made in and
# nothing else. The user layer is the one file that would carry a ceiling to every project on the
# machine, so no scope may write it - and a second repo must come out untouched.
home, repo = world()
other = scratch_dir()
os.makedirs(os.path.join(other, PROJECT_CONFIG_DIR))
subprocess.run(["git", "init", "-q", other], check=True, capture_output=True)
for scope, value in (("session", "+100_000"), ("project", "+100_000"), ("project", "off"),
                     ("project", "900_000"), ("session", "off")):
    code, out, err = run(home, repo, "set", scope, value)
    check(f"`set {scope} {value}` leaves the user layer alone",
          code == 0 and conf(home, CONFIG_NAME) is None, f"{code} {conf(home, CONFIG_NAME)}")
check("and leaves another project's layer alone",
      conf(other, PROJECT_CONFIG_DIR, CONFIG_NAME) is None, str(conf(other, PROJECT_CONFIG_DIR, CONFIG_NAME)))
check("having actually written the project it was run in",
      project_conf(repo) == "ceiling = 900000\n", str(project_conf(repo)))

# --- clear ------------------------------------------------------------------------------

home, repo = world(session_conf="ceiling = 400_000\n")
code, out, err = run(home, repo, "clear", "session")
check("clearing this session's layer removes the file",
      code == 0 and session_conf(home) is None, f"{code} {session_conf(home)}")
check("and returns the session to the ceiling beneath it",
      f"this session        {DEFAULT_CEILING:,} tokens" in out, out)
check("and says what the file had held, so the number is not lost with it",
      "400_000" in out, out)

home, repo = world(project_conf="ceiling = 350_000\n", session_conf="ceiling = 350000\n",
                   recorded=f"ceiling = {DEFAULT_CEILING}\n")
code, out, err = run(home, repo, "clear", "project")
check("clearing a project ceiling removes the project layer and this session's",
      code == 0 and project_conf(repo) is None and session_conf(home) is None,
      f"{code} {project_conf(repo)} {session_conf(home)}")
check("a new session here returns to the layer beneath the project",
      f"a new session here  {DEFAULT_CEILING:,} tokens" in out, out)
# The frozen record is the hook's, and clearing a layer does not reach into it.
check("and this session returns to the ceiling it started under, not to what the files say now",
      f"this session        {DEFAULT_CEILING:,} tokens" in out and conf(
          home, "sessions", SESSION, SHARED_AT_START) is not None, out)

home, repo = world()
code, out, err = run(home, repo, "clear", "session")
check("clearing a layer that was never written is not an error",
      code == 0 and "absent" in out, f"{code} {out}")

home, repo = world()
code, out, err = run(home, repo, "clear", "project")
check("clearing a project that never set a ceiling removes nothing and says so",
      code == 0 and "absent" in out and "project layer     (none)" in out, f"{code} {out}")

# Nothing stands, so nothing needs creating, so no repository root needs finding. A clear that
# borrowed the write site would refuse this - and refuse it talking about writing.
home, repo = world(git=False)
code, out, err = run(home, repo, "clear", "project")
check("clearing a project outside a git repository is not an error when nothing is there to clear",
      code == 0 and "absent" in out, f"{code} {out} {err}")

# The file the hook is dying on is the file `clear` exists to remove, so the reader that refuses it
# must not be the reader standing between the two.
home, repo = world(session_conf="ceiling = 400000\nceiling = 500000\n")
code, out, err = run(home, repo, "clear", "session")
check("a layer too broken to parse is still one this command can take away",
      code == 0 and session_conf(home) is None, f"{code} {session_conf(home)} {err}")
check("and it says what it could not read instead of dying on the way to the unlink",
      "unreadable" in out and "twice" in out, out)

# Tolerance goes exactly that far: a layer nobody asked to remove is still refused loudly, because
# a ceiling computed from a file that does not parse is a ceiling nobody set.
home, repo = world(user_conf="ceiling = 400000\nceiling = 500000\n")
code, out, err = run(home, repo, "show")
check("a malformed layer is still refused wherever a ceiling is resolved from it",
      code == 1 and "twice" in err, f"{code} {err}")

# --- the gate reads what this wrote -----------------------------------------------------

home, repo = world()
before, _ = gate(home, repo, 5_000_000)
check("a session far past the default is blocked before anything is moved",
      before == "block", str(before))
run(home, repo, "set", "session", "off")
after, ceiling = gate(home, repo, 5_000_000)
check("a session whose ceiling this command lifted is not blocked",
      after == "allow", f"{after} {ceiling}")

# The number the command printed and the number the gate enforced are the same number, measured
# at both ends. A project move is the case where they could differ, so it is the one tested.
home, repo = world()
code, out, err = run(home, repo, "set", "project", "400_000")
verdict, enforced = gate(home, repo, 450_000)
check("the gate enforces the ceiling a project move reported, to the token",
      enforced == 400_000 and verdict == "block", f"{out} -> {verdict} {enforced}")
verdict, enforced = gate(home, repo, 350_000, session="sess-3")
check("and a session under it is allowed",
      verdict == "allow" and enforced == 400_000, f"{verdict} {enforced}")

# The case the design is built around: a project move before this session has ever stopped. The
# hook's first stop freezes the shared layers, which now include the project file this command
# just wrote - so a session layer holding an *adjustment* would be applied on top of its own
# effect and land the session at twice the headroom anyone asked for.
home, repo = world()
code, out, err = run(home, repo, "set", "project", "+100_000")
check("a project move reports the headroom asked for, before any stop has happened",
      code == 0 and "this session        350,000 tokens" in out, f"{code} {out}")
verdict, enforced = gate(home, repo, 360_000)
check("and the gate's first stop enforces that, not twice the headroom",
      enforced == 350_000 and verdict == "block", f"{verdict} {enforced}")

# --- the shipped surface ----------------------------------------------------------------

check("the command is executable", os.access(CLI, os.X_OK), CLI)
check("the skill that documents it names it",
      "bin/ceiling" in open(os.path.join(os.path.dirname(HERE), "SKILL.md")).read(),
      os.path.join(os.path.dirname(HERE), "SKILL.md"))

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
