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
a shell-grammar parser, a git allowlist, the allowance that let ExitWorktree through the
refusal - went with it. The worktree guidance that allowance existed to serve did not: the
refusal a worktree session actually meets is the platform's, which no change here reaches,
so EXIT_HINT below carries it into the instruction this hook hands out at a stop.
"""

import fcntl
import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The config grammar and the layer order live in one module because two programs decide a
# ceiling from them - this hook, which reads the layers, and the `ceiling` command, which
# writes them. [LAW:one-source-of-truth] A plugin is a directory rather than an installed
# package, so the path comes before the import.
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "lib"))
from ceiling_config import (SHARED_AT_START, anchored, in_force,  # noqa: E402
                            session_directory, shared_at_start)

LOG_FILE = Path(os.environ.get("MEMENTO_CEILING_LOG")
                or Path.home() / ".claude" / "memento" / "context-ceiling.log")
LOG_CAP = 2_000_000
LAUNCHER = os.path.join(PLUGIN_ROOT, "skills", "message-in-a-bottle", "bin", "finalize-session")
# Matched rather than the absolute path, because the instruction hands out that path but an
# agent holding the skill may reach the launcher by name or through a wrapper. The basename is
# a substring of the path, so naming it covers both.
LAUNCHER_NAME = os.path.basename(LAUNCHER)
EVERY_PROMPT_COMPONENT = ("input_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens", "output_tokens")
TAIL_CHUNK = 256 * 1024
# The launcher's report of a scheduled reset, rendered - not the words `handoff scheduled`,
# which are prose about the launcher and appear in the skill the instruction below says to load
# and in the launcher's own source, so an agent that merely read about the close-out was
# credited with running it. The digits and the absolute log path are the parts a run fills in:
# the source carries `${HANDOFF_DELAY_SECONDS}s` and `$LOGFILE`, the prose carries `Ns` and
# `<tempfile>`, and neither renders. This is still only what the launcher emits and not proof
# that it ran, which is why `closed_out` asks for two more things. Cross-checked by the suite
# against a real
# rendered line, the launcher's source and that prose, because a rename in the launcher would
# otherwise leave every close-out uncredited and every session stuck at the block.
RESET_MARKER = re.compile(r"handoff scheduled → .* in \d+s \(log: /")
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

def resolve_ceiling(hook):
    """The ceiling in force for the session this payload belongs to.

    The project is anchored at the directory the session belongs to rather than wherever a Bash
    call last left it, which `anchored` decides for this and for the command that writes these
    layers. Reading the shared layers is also what records them for this session, which is why this
    runs at a stop and nowhere else."""
    anchor = anchored(hook["cwd"])
    directory = session_directory(hook["session_id"])
    return in_force(directory, shared_at_start(directory / SHARED_AT_START, anchor))

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

def launcher_ran(command):
    """Whether a Bash command invokes the launcher rather than reproducing its report.

    A launcher invocation cannot contain the launcher's report: the log path in that line comes
    from `mktemp` while the command runs, after the command was written. So a command carrying
    the rendered line is printing it - `echo`, `printf`, a heredoc - and a command naming the
    launcher without it is calling it. That is the whole of what the deleted shell-grammar
    parser was for, without the grammar, and so without its cost - it refused a legitimate
    `git commit -F -` and stranded the session that wrote it. [LAW:polishing-by-subtraction]

    The residue is a handoff message quoting a rendered line back verbatim, digits and absolute
    log path intact, which is not credited. That costs one re-blocked stop and the agent runs
    the close-out again, and this gate takes the harmless error where it has to choose.
    """
    return LAUNCHER_NAME in command and not RESET_MARKER.search(command)

def closed_out(transcript_path):
    """Whether the close-out ran in the turn now ending, and actually reset the session.

    [FRAMING:representation] a Bash call is the one place where the map and the territory have
    the same author: the agent writes the command AND thereby chooses what comes back, so neither
    string is evidence on its own. Three attempts here each trusted one of them - a shell-grammar
    parser precise enough to refuse a legitimate `git commit -F -`, a substring loose enough that
    `echo 'run finalize-session --reset compact'` credited a close-out that never ran, and then a
    rendered-line match that `echo 'handoff scheduled → tmux x:1 in 10s (log: /tmp/x)'` satisfied
    just as easily. So a close-out is three facts the transcript states outright: a result
    matching RESET_MARKER, the `tool_use_id` binding that result to a Bash call, and a command
    that `launcher_ran` reads as an invocation rather than a reproduction. Loading the skill the
    instruction names has none of them, reading the launcher's source has the second, and every
    way of printing the line - with or without the launcher's name alongside it - fails the third
    on the line's own presence in the command.

    What this cannot do is stop a session determined to counterfeit its own close-out: a script
    file that prints the line is a command with neither the line nor a lie in it. Nothing read
    out of a transcript the agent writes can close that, and pretending otherwise is what put
    three weaker checks here before this one. What it does close is every forgery cheap enough
    to happen without meaning it, which is the actual threat - a session that ran out of turns,
    got confused, or read the contract instead of running it.

    Records are scanned newest-first, so a result arrives before the call it belongs to; that is
    what `reported` is for - it carries the ids of reporting results across to their `tool_use`.

    Bounded at the turn, because crediting an older one would wave through every later breach in
    a transcript the tmux transport resets in place. An errored call is written into the
    transcript exactly like one that ran, so the error flag still decides before the text does.
    """
    reported = set()
    for record in records_newest_first(transcript_path):
        if starts_a_turn(record):
            return False
        content = record.get("message", {}).get("content")
        for block in reversed(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if (block.get("type") == "tool_result" and not block.get("is_error")
                    and block.get("tool_use_id")
                    and RESET_MARKER.search(result_text(block))):
                reported.add(block["tool_use_id"])
            elif (block.get("type") == "tool_use" and block.get("name") == "Bash"
                    and block.get("id") in reported
                    and launcher_ran((block.get("input") or {}).get("command", ""))):
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
