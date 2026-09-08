#!/usr/bin/env python3
"""The context ceiling: a session past the hard token maximum is told, once, to close out.

One clock, one checkpoint. The count is the newest assistant record's usage, and that
record describes the live context only at the moment a turn ends. The tmux transport
resets a session in place, so the transcript file and the session id outlive the context
they describe: mid-turn, before the new turn has produced a record of its own, the newest
one still belongs to the context that was just thrown away. Stop is the one event where
that cannot happen, so Stop is the only event this runs on.

This was also enforced on PreToolUse, to catch a session that runs a long tool loop and
never stops. It read the same number one tool call too early. A session that closed out at
479,224 tokens read 479,224 again on its first call after the reset, had its opening Skill
call denied, and died before doing any work. Measuring where the number is false cost more
than the sessions the second event was there to catch, and every gate that hung off it -
a shell-grammar parser, a git allowlist, a worktree escape hatch - went with it.
"""

import collections
import fcntl
import json
import math
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_CEILING = 250_000
# One filename at every layer, so a second setting is a new key rather than a new file, a new
# lookup and a new precedence chain. [LAW:composability]
CONFIG_NAME = "memento.conf"
# A repo carries its own config as a dot-directory, because a checkout has no XDG anything.
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CONFIG_HOME = Path(os.environ.get("MEMENTO_CONFIG_HOME") or XDG_CONFIG / "promptctl")
USER_CONFIG = CONFIG_HOME / CONFIG_NAME
SESSION_CONFIGS = CONFIG_HOME / "sessions"
PROJECT_CONFIG_DIR = ".promptctl"
CEILING_KEY = "ceiling"
DISABLING_WORD = "off"
# [0-9] rather than \d: `str.isdigit` was true of characters `int` then refused, so the guard
# and the conversion disagreed. Underscores group digits as Python's own literals do.
CEILING_RE = re.compile(r"(?P<sign>[+-]?)(?P<digits>[0-9]+(?:_[0-9]+)*)\Z")
# A ceiling as one layer wrote it, carrying the file and line a person goes to change it.
Written = collections.namedtuple("Written", "source text")
LOG_FILE = Path(os.environ.get("MEMENTO_CEILING_LOG")
                or Path.home() / ".claude" / "memento" / "context-ceiling.log")
LOG_CAP = 2_000_000
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(PLUGIN_ROOT, "skills", "message-in-a-bottle", "bin", "finalize-session")
EVERY_PROMPT_COMPONENT = ("input_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens", "output_tokens")
TAIL_CHUNK = 256 * 1024
# Recognised by the launcher only as its first argument: the launcher re-entering itself to
# do the delivery, which is not a close-out. Nothing spawns these through the Bash tool, but
# crediting one would let a stop proceed with no handoff written at all.
# What the launcher prints when it has scheduled a reset - the only thing that distinguishes a
# close-out from a call that recorded a handoff and carried on. Printed by the tmux, iTerm2 and
# detached branches and by nothing else, so it is the launcher saying so rather than this hook
# guessing. Cross-checked against the launcher's source by the suite, because a rename there
# would otherwise leave every close-out uncredited and every session stuck at the block.
RESET_MARKER = "handoff scheduled"
# Deleting the PreToolUse gate removed this hook's own refusal, not the platform's: a
# worktree-isolated session still cannot run a non-git command the platform cannot prove stays
# inside the worktree. Without this line such a session gets an instruction it may be unable to
# run and no way out - the stranded session this whole gate exists to prevent.
EXIT_HINT = ('In a worktree that command may be refused where you stand; run ExitWorktree with '
             'action "keep" first, and close out from the directory it returns you to.')

INSTRUCTION = """CONTEXT CEILING: this session is at ~{tokens:,} tokens, past the {ceiling:,} hard maximum. Close it out now so the next session can pick the work back up. Commit or push everything outstanding first - a handoff across a reset loses whatever is not committed - then run the close-out:
    {launcher} --reset compact '<handoff message>'
{exit_hint}
`--reset` is what makes a close-out reset the session; without it the handoff is only recorded and you carry on. Load Skill(memento:message-in-a-bottle) for the handoff contract. That message is the ONLY thing the next session wakes up with, so it says what you were doing, exactly where you stopped, and the next concrete step. Pass it as one single-quoted argument, writing an apostrophe as '\\''; newlines inside the quotes are fine. Do not start new work, and do not ask the user whether to finalize."""

def ceiling_in(path):
    """The ceiling one config file sets, or None. [LAW:no-silent-failure] a line that is not one
    exits here: a key that reads as a no-op is precisely the ceiling its author believes they
    set and did not."""
    found = None
    for number, line in enumerate(path.read_text().splitlines() if path.exists() else [], 1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        key, assigned, text = (part.strip() for part in stripped.partition("="))
        if not assigned or not text:
            sys.exit(f"memento config: {path} line {number} should read `key = value`, "
                     f"but reads {line.strip()!r}. Fix it or remove it.")
        if key != CEILING_KEY:
            sys.exit(f"memento config: {path} line {number} sets {key!r}, which memento has "
                     f"no such setting for. It reads: {CEILING_KEY}.")
        if found:
            sys.exit(f"memento config: {path} sets {key!r} twice, at {found.source} and "
                     f"line {number}. Keep the one you meant.")
        found = Written(f"{path} line {number}", text)
    return found

def project_ceiling(anchor):
    """The ceiling set by the nearest .promptctl/memento.conf at or above the project directory,
    so a subdirectory or a worktree inherits the repo above it. The user's own file is passed
    over where the walk finds it, because applying one file as two layers is the divergence
    [LAW:one-source-of-truth] exists to forbid."""
    start = Path(anchor).resolve()
    for directory in (start, *start.parents):
        candidate = directory / PROJECT_CONFIG_DIR / CONFIG_NAME
        if candidate.exists() and candidate.resolve() != USER_CONFIG.resolve():
            return ceiling_in(candidate)
    return None

def parse_ceiling(written):
    """One written ceiling, as the move it makes on the ceiling beneath it. The three things a
    person can write - a count, an adjustment, `off` - leave here as one thing, so the fold
    applies them in order with nothing left to dispatch on. [LAW:dataflow-not-control-flow]"""
    if written.text.lower() == DISABLING_WORD:
        return lambda beneath: math.inf
    shape = CEILING_RE.match(written.text)
    if not shape:
        sys.exit(f"memento config: {written.source} should hold a number of tokens, a signed "
                 f"adjustment like +100_000, or {DISABLING_WORD}, but reads "
                 f"{written.text!r}. Fix it or remove it.")
    magnitude = int(shape.group("digits").replace("_", ""))
    if not shape.group("sign"):
        return lambda beneath: magnitude
    moved = magnitude if shape.group("sign") == "+" else -magnitude
    return lambda beneath: beneath + moved

def session_config(session_id):
    """The session layer's path, for a session id that names one directory and nothing else.

    [LAW:parse-dont-validate] an id that is not a bare name reads a config from outside the tree
    - `Path.__truediv__` discards the left operand when the right is absolute, and follows `..`
    when it is not. Containment is asked of the resolved directory rather than of the spelling,
    because `..`, `./..` and `..//` all name the same place and such spellings do not form a
    list."""
    directory = (SESSION_CONFIGS / str(session_id)).resolve()
    if directory.parent != SESSION_CONFIGS.resolve():
        sys.exit(f"memento config: session id {session_id!r} names {directory}, which is not "
                 f"a session directory under {SESSION_CONFIGS}. Memento cannot tell which "
                 f"session's settings it was meant to read.")
    return directory / CONFIG_NAME

def resolve_ceiling(hook):
    """The ceiling in force, folded from the least specific layer to the most.

    [LAW:single-enforcer] the one place the order between the layers is decided. The project is
    anchored at the directory the session belongs to rather than wherever a Bash call last left
    it - a ceiling that moved because something ran `cd` would be a ceiling nobody set."""
    anchor = os.environ.get("CLAUDE_PROJECT_DIR") or hook["cwd"]
    layers = (ceiling_in(USER_CONFIG), project_ceiling(anchor),
              ceiling_in(session_config(hook["session_id"])))
    written = [one for one in layers if one]
    ceiling = DEFAULT_CEILING
    for setting in written:
        ceiling = parse_ceiling(setting)(ceiling)
    if ceiling < 0:
        sys.exit(f"memento config: {', then '.join(one.source for one in written)} resolve to "
                 f"a ceiling of {ceiling:,} tokens, and a count of tokens is never negative.")
    return ceiling

def records_newest_first(transcript_path):
    """This session's records, reading only as far back as the caller consumes. Sidechains are
    a subagent's conversation, so they never reach a caller. A torn final line is ordinary in a
    transcript still being appended to, so an unparseable one is skipped rather than raised."""
    with open(transcript_path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        end, straddling_head = handle.tell(), b""
        while end > 0:
            start = max(0, end - TAIL_CHUNK)
            handle.seek(start)
            lines = (handle.read(end - start) + straddling_head).split(b"\n")
            straddling_head = b"" if start == 0 else lines.pop(0)
            for line in reversed(lines):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not record.get("isSidechain"):
                    yield record
            end = start

def context_tokens(transcript_path):
    for record in records_newest_first(transcript_path):
        if record.get("type") == "assistant":
            usage = record["message"].get("usage")
            if usage:
                return sum(usage.get(field, 0) for field in EVERY_PROMPT_COMPONENT)
    return 0

def starts_a_turn(record):
    content = record.get("message", {}).get("content")
    blocks = content if isinstance(content, list) else []
    return record.get("type") == "user" and not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks)

def result_text(block):
    """A tool result's text. The content is a plain string or a list of blocks depending on how
    the tool returned, and a close-out is credited off what it says, so both shapes are read."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") for part in content or []
                    if isinstance(part, dict))

def closed_out(transcript_path):
    """Whether the close-out ran in the turn now ending, and actually reset the session.

    [FRAMING:representation] the command string is a map of what a call was meant to do; the
    result is the territory. Reading the map is what went wrong here twice - first a shell-grammar
    parser precise enough to refuse a legitimate `git commit -F -`, then a substring loose enough
    that `echo 'run finalize-session --reset compact'` credited a close-out that never ran. Both
    were guesses about a command. RESET_MARKER is not a guess: the launcher prints it, from the
    three transport branches that schedule a reset and nowhere else, so a result carrying it is
    the launcher's own report that it ran, in launcher mode, and reset the session. A call that
    only records a handoff says `handoff recorded` instead, and is correctly not credited.

    Bounded at the turn, because crediting an older one would wave through every later breach in
    a transcript the tmux transport resets in place. An errored call is written into the
    transcript exactly like one that ran, so the error flag still decides before the text does.
    """
    for record in records_newest_first(transcript_path):
        if starts_a_turn(record):
            return False
        content = record.get("message", {}).get("content")
        for block in reversed(content if isinstance(content, list) else []):
            if (isinstance(block, dict) and block.get("type") == "tool_result"
                    and not block.get("is_error")
                    and RESET_MARKER in result_text(block)):
                return True
    return False

def log(hook, tokens, ceiling, verdict):
    """[LAW:no-silent-failure] a hook that allows emits nothing, and so does one that never ran;
    the log is the only place that difference exists. Its own failure is reported but not fatal
    - raising would take the gate down with the instrumentation. The ceiling is logged because
    it is folded from layers, so the line has to say which number won."""
    line = (f"{datetime.now().isoformat(timespec='seconds')} "
            f"session={str(hook.get('session_id'))[:8]} event={hook.get('hook_event_name')} "
            f"tokens={tokens} ceiling={ceiling} "
            f"-> {verdict}\n")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if os.fstat(handle.fileno()).st_size > LOG_CAP:
                handle.truncate(0)
                handle.write(f"[truncated at {LOG_CAP} bytes]\n")
            handle.write(line)
    except OSError as failure:
        print(f"memento context ceiling: cannot write {LOG_FILE}: {failure}", file=sys.stderr)

def stop(hook, tokens, ceiling):
    """Blocked once, never twice: a second block spends more context on the problem that IS too
    much context."""
    if closed_out(hook["transcript_path"]):
        return "closed-out", {"systemMessage": f"memento: the close-out ran at ~{tokens:,} "
                                               f"tokens, past the {ceiling:,} ceiling, so "
                                               f"the stop proceeds."}
    if hook.get("stop_hook_active"):
        return "spent", {"systemMessage": f"memento: context ceiling breached (~{tokens:,} > "
                                          f"{ceiling:,}) and this session has spent its one "
                                          f"forced close-out attempt, so the stop proceeds. "
                                          f"If the close-out did not run, the next session "
                                          f"starts with nothing."}
    return "block", {"decision": "block", "reason": INSTRUCTION.format(
        tokens=tokens, ceiling=ceiling, launcher=shlex.quote(LAUNCHER), exit_hint=EXIT_HINT)}

# The ceiling is read from the payload's session and project, so it is resolved here rather
# than at import: what it depends on does not exist until stdin has been read. The transcript
# is measured first so a payload missing it is named by the field it is missing.
hook = json.load(sys.stdin)
# [LAW:no-silent-failure] any other event is hooks.json having drifted from this file, and the
# number this reads is only true at a stop - so it says so rather than measuring anyway.
if hook["hook_event_name"] != "Stop":
    sys.exit(f"memento context ceiling: registered on Stop, called on "
             f"{hook['hook_event_name']}. Fix hooks.json.")
tokens = context_tokens(hook["transcript_path"])
ceiling = resolve_ceiling(hook)

label, verdict = ("allow-under", None) if tokens < ceiling else stop(hook, tokens, ceiling)
log(hook, tokens, ceiling, label)
if verdict:
    print(json.dumps(verdict))
