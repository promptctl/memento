#!/usr/bin/env python3
"""Unit tests for the `ceiling` command.

Each case runs the real command as a subprocess against a scratch config home and git repo, then
asserts on the files written and the output printed. The cases that matter most also run the real
Stop hook and check that it resolves the same ceiling the command reported. Run with: python3 ceiling.test.py
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
# The suite's environment is PATH of the one it runs in and SCRATCH as HOME, nothing else, so no
# developer's settings reach a test, every path derived from the home - the plugin install's among
# them - lands in scratch, and every command run here is given that and what its case sets. A git
# hook's environment names its repository in GIT_DIR, and a git command that inherited it would act
# on that repository.
path = os.environ["PATH"]
os.environ.clear()
os.environ.update(PATH=path, HOME=SCRATCH)

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "ceiling")
PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
HOOK = os.path.join(PLUGIN, "hooks", "scripts", "context-ceiling.py")
# The file-format constants come from the module the command and the hook both use, so fixtures
# match the files production writes.
sys.path.insert(0, os.path.join(PLUGIN, "lib"))
from ceiling_config import CONFIG_NAME, DEFAULT_CEILING, PROJECT_CONFIG_DIR  # noqa: E402

# Derived from the shipped default, so changing the default updates the expectation too.
RAISED = DEFAULT_CEILING + 100_000

SESSION = "sess-1"
failures = []


def check(name, condition, detail=""):
    print(f"ok   - {name}" if condition else f"FAIL - {name}: {detail}")
    if not condition:
        failures.append(name)


def scratch_dir():
    return tempfile.mkdtemp(dir=SCRATCH)


def world(user_conf=None, project_conf=None, session_conf=None, git=True):
    """Creates a config home and a repo with a `sub` directory, and writes each given layer to its file.

    Layers go through real files rather than flags, so the tests exercise the paths the command
    resolves. Returns (home, repo)."""
    home, repo = scratch_dir(), scratch_dir()
    os.makedirs(os.path.join(repo, "sub"))
    if git:
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
    for path, text in ((os.path.join(home, CONFIG_NAME), user_conf),
                       (os.path.join(repo, PROJECT_CONFIG_DIR, CONFIG_NAME), project_conf),
                       (os.path.join(home, "sessions", SESSION, CONFIG_NAME), session_conf)):
        if text is not None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(text)
    return home, repo


def run(home, cwd, *argv, session=SESSION, anchor=None, pythonpath=None, shadowing=None):
    """Runs the command as the skill does.

    `anchor` sets CLAUDE_PROJECT_DIR, for cases where the session's project differs from the working directory."""
    env = dict(os.environ, MEMENTO_CONFIG_HOME=home)
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    if anchor is not None:
        env["CLAUDE_PROJECT_DIR"] = anchor
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    if shadowing is not None:
        env["PATH"] = f"{shadowing}:{env['PATH']}"
    done = subprocess.run([sys.executable, CLI, *argv], text=True, capture_output=True,
                          env=env, cwd=cwd)
    return done.returncode, done.stdout, done.stderr


def aged_argparse():
    """Returns a directory whose sitecustomize.py restores the pre-3.13 argparse negative-number matcher.

    On 3.13 and later, `-50_000` parses without any guard in the command, so placing this on PYTHONPATH
    is what makes the guard observable."""
    directory = scratch_dir()
    with open(os.path.join(directory, "sitecustomize.py"), "w") as handle:
        handle.write("import argparse, re\n"
                     "_born = argparse._ActionsContainer.__init__\n"
                     "def aged(self, *a, **kw):\n"
                     "    _born(self, *a, **kw)\n"
                     r"    self._negative_number_matcher = re.compile(r'^-\d+$|^-\d*\.\d+$')"
                     "\nargparse._ActionsContainer.__init__ = aged\n")
    return directory


def meddling_writer(text, target=None):
    """Returns a directory whose sitecustomize.py writes `text` between the command's staging step and its comparison.

    This makes the race happen on every run. With no `target`, the text lands on each destination. A named
    `target` is a layer the command reads but does not write, which exercises the second resolution."""
    directory = scratch_dir()
    with open(os.path.join(directory, "sitecustomize.py"), "w") as handle:
        handle.write(f"import sys\nsys.path.insert(0, {os.path.join(PLUGIN, 'lib')!r})\n"
                     "import ceiling_config\n"
                     "from pathlib import Path\n"
                     f"_target = Path({target!r}) if {target!r} else None\n"
                     "_staged = ceiling_config.staged\n"
                     "def meddled(path, ceiling):\n"
                     "    partial = _staged(path, ceiling)\n"
                     f"    (_target or path).write_text({text!r})\n"
                     "    return partial\n"
                     "ceiling_config.staged = meddled\n")
    return directory


ECHOING_GIT = """#!/bin/sh
# Echoes any `--` option back and exits 0, as an old `git rev-parse` does for unknown options.
for argument in "$@"; do
  case "$argument" in --*) echo "$argument" ;; esac
done
exit 0
"""


def stub_git(behaviour):
    """Returns a directory holding a `git` with the given script, for placing first on PATH."""
    directory = scratch_dir()
    standing_in = os.path.join(directory, "git")
    with open(standing_in, "w") as handle:
        handle.write(behaviour)
    os.chmod(standing_in, 0o755)
    return directory


def repo_with_commit(path):
    """A repository holding one commit, which is what adding a worktree or a submodule needs."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master", path], check=True, capture_output=True)
    for key, value in (("user.email", "test@example.invalid"), ("user.name", "ceiling test")):
        subprocess.run(["git", "config", key, value], cwd=path, check=True, capture_output=True)
    with open(os.path.join(path, "README"), "w") as handle:
        handle.write("x\n")
    subprocess.run(["git", "add", "README"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=path, check=True, capture_output=True)
    return path


def gate(home, repo, tokens, session=SESSION):
    """Runs the real Stop hook at a token count. Returns (verdict, ceiling), read from the hook's log line."""
    transcript = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir=SCRATCH)
    transcript.write(json.dumps({"type": "user", "isSidechain": False,
                                 "message": {"content": "hi"}}) + "\n")
    transcript.write(json.dumps({"type": "assistant", "isSidechain": False, "message": {"usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": tokens - 2, "output_tokens": 0}}}) + "\n")
    transcript.close()
    log = os.path.join(scratch_dir(), "ceiling.log")
    env = dict(os.environ, MEMENTO_CONFIG_HOME=home, MEMENTO_CEILING_LOG=log)
    done = subprocess.run([sys.executable, HOOK], text=True, capture_output=True, env=env,
                          cwd=repo, input=json.dumps(
                              {"session_id": session, "hook_event_name": "Stop", "cwd": repo,
                               "transcript_path": transcript.name, "stop_hook_active": False}))
    if done.returncode != 0:
        return f"exit {done.returncode}: {done.stderr.strip()}", None
    emitted = json.loads(done.stdout) if done.stdout.strip() else None
    line = open(log).read().strip().split("\n")[-1]
    verdict = "block" if emitted and emitted.get("decision") == "block" else "allow"
    # A lifted ceiling is logged as `inf`, so the value is read as a float.
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
      all(label in out for label in ("session layer", "project layer", "user layer"))
      and out.count("(unset)") == 2, out)
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
      code == 0 and f"{RAISED:,} tokens" in out, f"{code} {out} {err}")
check("the session layer holds the resolved number, which is what the hook reads back",
      session_conf(home) == f"ceiling = {RAISED}\n", session_conf(home))
check("a session-scoped move leaves the project alone", project_conf(repo) is None,
      str(project_conf(repo)))
# The number a new session here would get is the one a session-scoped move must NOT touch.
check("and show still reports the default for a new session here",
      f"a new session here  {DEFAULT_CEILING:,} tokens" in out, out)

# A second move starts from the first, because `+50_000` means more room than I have now.
code, out, err = run(home, repo, "set", "session", "+50_000")
check("a second signed move starts from the ceiling now in force",
      code == 0 and f"{RAISED + 50_000:,} tokens" in out
      and session_conf(home) == f"ceiling = {RAISED + 50_000}\n",
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

# `-50_000` against an argparse that treats a leading `-` as an option, as Python before 3.13 does.
home, repo = world()
code, out, err = run(home, repo, "set", "session", "-50_000", pythonpath=aged_argparse())
check("a negative adjustment is a value and not an option on every interpreter",
      code == 0 and session_conf(home) == f"ceiling = {DEFAULT_CEILING - 50_000}\n",
      f"{code} {out} {err}")
code, out, err = run(home, repo, "set", "session", pythonpath=aged_argparse())
check("and a value left off there is still an invocation error rather than a silent default",
      code == 2, f"{code} {err}")

# The one token that can stand in the value slot and not be a value. Quoted along with the rest, a
# request for help is answered by a complaint about a ceiling spelled `-h`.
home, repo = world()
code, out, err = run(home, repo, "set", "project", "-h")
check("asking for help where the value goes is answered with help, not with a grammar error",
      code == 0 and "off|N|+N|-N" in out, f"{code} {out} {err}")
check("and that answer writes nothing",
      project_conf(repo) is None and session_conf(home) is None,
      f"{project_conf(repo)} {session_conf(home)}")
code, out, err = run(home, repo, "set", "--help", pythonpath=aged_argparse())
check("the same request one token earlier still reaches argparse",
      code == 0 and "{project,session}" in out, f"{code} {out} {err}")

# --- set project ------------------------------------------------------------------------

home, repo = world()
code, out, err = run(home, repo, "set", "project", "+100_000")
check("a project-scoped move writes the project layer",
      code == 0 and project_conf(repo) == f"ceiling = {RAISED}\n", str(project_conf(repo)))
check("and nothing else - this session's layer stays untouched", session_conf(home) is None,
      str(session_conf(home)))
# Every layer is read live, so the one file reaches this session and the next alike.
check("so this session and a new session here run under the same ceiling",
      f"this session        {RAISED:,} tokens" in out
      and f"a new session here  {RAISED:,} tokens" in out, out)
check("the command names the one file it wrote", out.count("wrote ") == 1, out)

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
# Asserts git's own error text, which the command passes through.
check("outside a git repository a project move refuses rather than guessing a root",
      code == 1 and "fatal: not a git repository" in err, f"{code} {err}")
check("and writes nothing, including the session layer",
      project_conf(repo) is None and session_conf(home) is None,
      f"{project_conf(repo)} {session_conf(home)}")

# A git too old for a question echoes it back as the answer. The command must refuse that rather than
# write a file named after the flag.
home, repo = world()
code, out, err = run(home, repo, "set", "project", "300_000", shadowing=stub_git(ECHOING_GIT))
check("a git that hands a question back has not answered it",
      code == 1 and "by handing it back" in err, f"{code} {err}")
check("and no config lands in a directory named after a flag",
      project_conf(repo) is None and session_conf(home) is None
      and [name for name in os.listdir(repo) if name.startswith("--")] == [],
      f"{project_conf(repo)} {sorted(os.listdir(repo))}")

# The project is the anchored one, not the working directory. Other cases run with both in one place,
# so this case is the one that tells them apart.
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

# What a move replaces is printed as it goes, the way `clear` prints what it removes.
home, repo = world(project_conf="ceiling = 900_000\n")
code, out, err = run(home, repo, "set", "project", "400_000")
check("a move says what the file held, so the number is not lost silently",
      code == 0 and "replacing 900_000" in out, out)

# A project move ignores the session's own layer, which stays in force over the result.
home, repo = world(session_conf="ceiling = 900_000\n")
code, out, err = run(home, repo, "set", "project", "+100_000")
check("a project move starts from the project, not from the room one session gave itself",
      code == 0 and project_conf(repo) == f"ceiling = {RAISED}\n", str(project_conf(repo)))
check("and leaves that session's own layer in force over it",
      session_conf(home) == "ceiling = 900_000\n" and "this session        900,000 tokens" in out,
      f"{session_conf(home)} {out}")

# --- one project, and no further ---------------------------------------------------------

# A change reaches only the project it was made in. No scope may write the user layer, and another
# project must stay untouched.
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

home, repo = world(project_conf="ceiling = 350_000\n", session_conf="ceiling = +50_000\n")
code, out, err = run(home, repo, "clear", "project")
check("clearing a project ceiling removes the project layer and only that",
      code == 0 and project_conf(repo) is None and session_conf(home) == "ceiling = +50_000\n",
      f"{code} {project_conf(repo)} {session_conf(home)}")
check("a new session here returns to the layer beneath the project",
      f"a new session here  {DEFAULT_CEILING:,} tokens" in out, out)
check("and this session's own layer now sits on the layer beneath the project",
      f"this session        {DEFAULT_CEILING + 50_000:,} tokens" in out, out)

home, repo = world()
code, out, err = run(home, repo, "clear", "session")
check("clearing a layer that was never written is not an error",
      code == 0 and "absent" in out, f"{code} {out}")

home, repo = world()
code, out, err = run(home, repo, "clear", "project")
check("clearing a project that never set a ceiling removes nothing and says so",
      code == 0 and "absent" not in out and "removed" not in out
      and "project layer     (none)" in out, f"{code} {out}")

# With nothing to remove, no repository root is needed. A clear that asked for one would refuse with a
# message about writing.
home, repo = world(git=False)
code, out, err = run(home, repo, "clear", "project")
check("clearing a project outside a git repository is not an error when nothing is there to clear",
      code == 0 and "project layer     (none)" in out, f"{code} {out} {err}")

# `clear` must not parse the file it removes, since parsing is what fails on a broken layer.
home, repo = world(session_conf="ceiling = 400000\nceiling = 500000\n")
code, out, err = run(home, repo, "clear", "session")
check("a layer too broken to parse is still one this command can take away",
      code == 0 and session_conf(home) is None, f"{code} {session_conf(home)} {err}")
check("and it says what it could not read instead of dying on the way to the unlink",
      "unreadable" in out and "twice" in out, out)

# Only `clear` tolerates a broken layer. Any command that resolves a ceiling from it still refuses.
home, repo = world(user_conf="ceiling = 400000\nceiling = 500000\n")
code, out, err = run(home, repo, "show")
check("a malformed layer is still refused wherever a ceiling is resolved from it",
      code == 1 and "twice" in err, f"{code} {err}")

# A file whose bytes are not text is also one `clear` must be able to remove.
home, repo = world()
bad_bytes = os.path.join(home, "sessions", SESSION, CONFIG_NAME)
os.makedirs(os.path.dirname(bad_bytes), exist_ok=True)
with open(bad_bytes, "wb") as handle:
    handle.write(b"ceiling = 35\xff0000\n")
code, out, err = run(home, repo, "clear", "session")
check("a layer whose bytes are not text is one this command can still take away",
      code == 0 and not os.path.exists(bad_bytes), f"{code} {err}")
check("and it says what it could not read on the way out", "unreadable" in out, out)

# Every reader but `held` reaches the parser directly - the report's own layer lines among them - so
# a layer nobody is removing has to refuse in the parser's voice rather than as a traceback.
home, repo = world()
with open(os.path.join(home, CONFIG_NAME), "wb") as handle:
    handle.write(b"ceiling = 35\xff0000\n")
code, out, err = run(home, repo, "show")
check("a layer of bytes that are not text refuses where a ceiling is resolved from it",
      code == 1 and "not text" in err and "Traceback" not in err, f"{code} {err}")

home, repo = world(session_conf="ceiling = 400000\n")
unopenable = os.path.join(home, "sessions", SESSION, CONFIG_NAME)
os.chmod(unopenable, 0o000)
code, out, err = run(home, repo, "clear", "session")
check("so is one the filesystem will not open", code == 0 and not os.path.exists(unopenable),
      f"{code} {err}")
check("and that one says what it could not read too", "unreadable" in out, out)

# --- a layer replaced, rather than read ---------------------------------------------------

# A count states a ceiling outright and never reads the layer it replaces, so `set` can overwrite a
# layer that does not parse.
BROKEN = "ceiling = 400000\nceiling = 500000\n"
home, repo = world(session_conf=BROKEN)
code, out, err = run(home, repo, "set", "session", "400_000")
check("an absolute move replaces a layer too broken to parse instead of dying on it",
      code == 0 and session_conf(home) == "ceiling = 400000\n", f"{code} {out} {err}")

home, repo = world(session_conf=BROKEN)
code, out, err = run(home, repo, "set", "session", "off")
check("and off replaces it too, for the same reason", code == 0 and session_conf(home) ==
      "ceiling = off\n", f"{code} {out} {err}")

# An adjustment needs the number beneath, and a file that does not parse has none to give.
home, repo = world(session_conf=BROKEN)
code, out, err = run(home, repo, "set", "session", "+50_000")
check("an adjustment over the same file still refuses, having no base to add to",
      code == 1 and "twice" in err, f"{code} {err}")
check("and leaves that file exactly as it found it", session_conf(home) == BROKEN,
      str(session_conf(home)))

# The project layer is the shared one, and the same asymmetry applies to it.
home, repo = world(project_conf=BROKEN)
code, out, err = run(home, repo, "set", "project", "300_000")
check("an absolute project move replaces a malformed project layer",
      code == 0 and project_conf(repo) == "ceiling = 300000\n", f"{code} {out} {err}")

# --- a destination that moved under the command -------------------------------------------

# The project layer is shared, so two sessions can adjust it at once. Each would read the same number,
# and the later write would drop the earlier change without either report mentioning it.
home, repo = world(session_conf="ceiling = 900000\n")
code, out, err = run(home, repo, "set", "session", "400_000",
                     pythonpath=meddling_writer("ceiling = 777000\n"))
check("a destination that changed since it was read stops the write",
      code == 1 and "changed while" in err, f"{code} {out} {err}")
check("and the change that landed there is the one left standing",
      session_conf(home) == "ceiling = 777000\n", str(session_conf(home)))

# A write rests on a number, and some layers beneath it are not destinations. The user layer can
# change while no destination does, so only the second resolution catches that.
home, repo = world(user_conf="ceiling = 300000\n")
code, out, err = run(home, repo, "set", "project", "+50_000",
                     pythonpath=meddling_writer("ceiling = 900000\n",
                                                target=os.path.join(home, CONFIG_NAME)))
check("a base that moved while the move was being worked out stops the write",
      code == 1 and "resolved to 350,000 tokens and then to 950,000 tokens" in err,
      f"{code} {out} {err}")
check("and neither destination is written",
      project_conf(repo) is None and session_conf(home) is None,
      f"{project_conf(repo)} {session_conf(home)}")

# A count reads no base, so a change beneath it does not stop the write.
home, repo = world(user_conf="ceiling = 300000\n")
code, out, err = run(home, repo, "set", "project", "300_000",
                     pythonpath=meddling_writer("ceiling = 900000\n",
                                                target=os.path.join(home, CONFIG_NAME)))
check("a count asks for no base, so a change underneath it writes anyway",
      code == 0 and project_conf(repo) == "ceiling = 300000\n", f"{code} {out} {err}")

# --- nothing staged left behind -----------------------------------------------------------

# A leftover `memento.conf.<pid>` is read by nothing here, so the test checks that none remains. The
# staging write is refused by a read-only directory.
home, repo = world()
holding = os.path.join(repo, PROJECT_CONFIG_DIR)
os.makedirs(holding)
os.chmod(holding, 0o500)
try:
    code, out, err = run(home, repo, "set", "project", "300_000")
finally:
    os.chmod(holding, 0o700)
left = sorted(os.listdir(holding))
check("a staging pass that cannot finish leaves nothing of itself behind",
      code != 0 and left == [], f"{code} {left}")
check("and none of the move is in place", project_conf(repo) is None, str(project_conf(repo)))
check("and it refuses in this command's own shape rather than as a traceback",
      code == 1 and err.startswith("ceiling: ") and "Traceback" not in err, f"{code} {err}")

# --- a filesystem that said no ------------------------------------------------------------

# unlink needs write permission on the parent directory, not the file. The command must report that
# as its own error, not as a traceback.
home, repo = world(project_conf="ceiling = 350000\n")
holding = os.path.join(repo, PROJECT_CONFIG_DIR)
os.chmod(holding, 0o500)
try:
    code, out, err = run(home, repo, "clear", "project")
finally:
    os.chmod(holding, 0o700)
check("a removal the filesystem refuses is this command's refusal, not a traceback",
      code == 1 and err.startswith("ceiling: ") and "Traceback" not in err, f"{code} {err}")
check("and the file it could not remove is left as it was",
      project_conf(repo) == "ceiling = 350000\n", str(project_conf(repo)))

# The same class of failure reached through a read, in the command that writes nothing at all.
home, repo = world(user_conf="ceiling = 350000\n")
shut = os.path.join(home, CONFIG_NAME)
os.chmod(shut, 0o000)
try:
    code, out, err = run(home, repo, "show")
finally:
    os.chmod(shut, 0o600)
check("a layer the filesystem will not open refuses the same way",
      code == 1 and err.startswith("ceiling: ") and "Traceback" not in err, f"{code} {err}")

# --- which checkout the project is --------------------------------------------------------

# A worktree's project is the checkout it belongs to, since a file at the worktree's root stops
# applying once the worktree is deleted.
home = scratch_dir()
main = repo_with_commit(os.path.join(scratch_dir(), "main"))
linked = os.path.join(scratch_dir(), "linked")
subprocess.run(["git", "worktree", "add", "-q", linked, "-b", "side"], cwd=main, check=True,
               capture_output=True)
code, out, err = run(home, linked, "set", "project", "300_000")
check("a project move from a linked worktree writes the checkout it belongs to",
      code == 0 and project_conf(main) == "ceiling = 300000\n", f"{code} {out} {err}")
check("and leaves no second project file at the worktree's own root",
      project_conf(linked) is None, str(project_conf(linked)))

# A submodule's git dir sits in the superproject's storage, which no walk up from the submodule
# reaches. The project file must be written at the submodule's own root.
home = scratch_dir()
superproject = repo_with_commit(os.path.join(scratch_dir(), "super"))
vendored = repo_with_commit(os.path.join(scratch_dir(), "vendored"))
subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", vendored,
                "vendor"], cwd=superproject, check=True, capture_output=True)
inside = os.path.join(superproject, "vendor")
code, out, err = run(home, inside, "set", "project", "300_000")
check("a project move inside a submodule writes the submodule's own root",
      code == 0 and project_conf(inside) == "ceiling = 300000\n", f"{code} {out} {err}")
check("and writes nothing into the superproject's git storage",
      not os.path.exists(os.path.join(superproject, ".git", "modules", PROJECT_CONFIG_DIR)),
      os.path.join(superproject, ".git", "modules", PROJECT_CONFIG_DIR))
verdict, enforced = gate(home, inside, 310_000, session="sub-1")
check("and the hook reads that file back from inside the submodule",
      enforced == 300_000 and verdict == "block", f"{verdict} {enforced}")

# --- the gate reads what this wrote -----------------------------------------------------

home, repo = world()
before, _ = gate(home, repo, 5_000_000)
check("a session far past the default is blocked before anything is moved",
      before == "block", str(before))
run(home, repo, "set", "session", "off")
after, ceiling = gate(home, repo, 5_000_000)
check("a session whose ceiling this command lifted is not blocked",
      after == "allow", f"{after} {ceiling}")

# The ceiling the command reports must match the ceiling the gate enforces. A project move is tested
# because it is the case where they could diverge.
home, repo = world()
code, out, err = run(home, repo, "set", "project", "400_000")
verdict, enforced = gate(home, repo, 450_000)
check("the gate enforces the ceiling a project move reported, to the token",
      enforced == 400_000 and verdict == "block", f"{out} -> {verdict} {enforced}")
verdict, enforced = gate(home, repo, 350_000, session="sess-3")
check("and a session under it is allowed",
      verdict == "allow" and enforced == 400_000, f"{verdict} {enforced}")

# The hook reads the layers live, so a project move reaches a session at its next stop, including a
# session that has already stopped once.
home, repo = world()
verdict, enforced = gate(home, repo, RAISED + 10_000)
check("a session's stop before the move reads the default", enforced == DEFAULT_CEILING, str(enforced))
code, out, err = run(home, repo, "set", "project", "+100_000")
check("a project move reports the headroom asked for",
      code == 0 and f"this session        {RAISED:,} tokens" in out, f"{code} {out}")
verdict, enforced = gate(home, repo, RAISED + 10_000)
check("and that session's next stop enforces it, to the token",
      enforced == RAISED and verdict == "block", f"{verdict} {enforced}")

# --- the shipped surface ----------------------------------------------------------------

check("the command is executable", os.access(CLI, os.X_OK), CLI)
check("the skill that documents it names it",
      "bin/ceiling" in open(os.path.join(os.path.dirname(HERE), "SKILL.md")).read(),
      os.path.join(os.path.dirname(HERE), "SKILL.md"))

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
