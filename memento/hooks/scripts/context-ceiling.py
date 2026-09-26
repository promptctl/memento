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

import collections
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
from ceiling_config import (GRACE, SHARED_AT_START, anchored, in_force,  # noqa: E402
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

# What a block past the ceiling says, by how far past it the session is. A close-out forced in the
# middle of a unit hands the next session half a task to reread from the start, so up to the limit
# the agent finishes the unit it is in, and from the limit on it closes out wherever it stands.
Band = collections.namedtuple("Band", "label reason spent")

# The phrase that identifies a finishing block when a later stop reads its turn opener back off the
# transcript (the harness echoes each block as `Stop hook feedback:`). [LAW:one-source-of-truth] it
# is spliced into FINISHING's reason below, so the phrase the escalation matches is the phrase the
# agent was shown and the two cannot drift. Only a finishing opener is ever matched - a closing
# opener is the default the escalation need not name - so CLOSING carries no such phrase.
FINISHING_MARK = "Finish the unit of work you are in the middle of"

# The close-out mechanics both bands end on - the command, the worktree caveat, and the handoff
# contract - live here once. [LAW:one-source-of-truth] a change to how the close-out is run or
# described lands in both reasons at once instead of drifting between two copies. Each band's reason
# is its own opening plus this, plus the one clause that differs: what not to do while closing out.
CLOSE_OUT = """
    {launcher} '<handoff message>'
{exit_hint}
Running it records the handoff and resets this session into it. Load Skill(memento:message-in-a-bottle) for the handoff contract. That message is the ONLY thing the next session wakes up with, so it says what you were doing, exactly where you stopped, and the next concrete step. Pass it as one single-quoted argument, writing an apostrophe as '\\''; newlines inside the quotes are fine. """

FINISHING = Band("finish",
    "CONTEXT CEILING: this session is at ~{tokens:,} tokens, past the {ceiling:,} ceiling; {limit:,} is the hard limit. "
    + FINISHING_MARK
    + """ - the PR, the ticket, the task you were handed - and close out the moment it is done. Do not start another unit: the thought "the next ticket is small, I'll take it too" is starting one, and it belongs to the next session. If the unit is already done, or you are between units, close out now. If you are mid-unit and ended this turn only to wait on something (a background task, CI), end your turn again: that stop goes through, unless the session has reached {limit:,} by then, in which case this hook blocks it once more to make you close out wherever you stand. You do not track the count - this hook does. To close out, commit or push everything outstanding first - a handoff across a reset loses whatever is not committed - then run:""" + CLOSE_OUT
    + "Do not move the ceiling to make room, and do not ask the user whether to finalize.",
    "memento: this session is past the {ceiling:,} ceiling at ~{tokens:,} tokens and stopped again "
    "without closing out, so the stop proceeds. It may finish the unit of work it is in; at "
    "{limit:,} it is made to close out.")

CLOSING = Band("block",
    """CONTEXT CEILING: this session is at ~{tokens:,} tokens, past the {ceiling:,} ceiling and the {limit:,} hard limit. Close it out now so the next session can pick the work back up. Commit or push everything outstanding first - a handoff across a reset loses whatever is not committed - then run the close-out:""" + CLOSE_OUT
    + "Do not start new work, do not move the ceiling to make room, and do not ask the user whether to finalize.",
    "memento: context ceiling breached (~{tokens:,} > {ceiling:,}) and this session has spent its "
    "one forced close-out attempt, so the stop proceeds. If the close-out did not run, the next "
    "session starts with nothing.")

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

def text_of(content):
    """The text of a record's or block's `content`, which is a plain string or a list of blocks
    depending on how it was written. [LAW:one-source-of-truth] both readers of a content field -
    a tool result credited as a close-out and the opener a turn is escalated on - read it here, so
    a list-shaped content is never mistaken for one with no text."""
    if isinstance(content, str):
        return content
    return " ".join(block.get("text", "") for block in content or []
                    if isinstance(block, dict))

def result_text(block):
    """A tool result's text, off which a close-out is credited."""
    return text_of(block.get("content"))

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

def turn_opening(transcript_path):
    """The text of the record that started the turn now ending, or "" for one with no text.

    A block reaches the agent as a user record reading `Stop hook feedback:` and the reason, which
    is a record `starts_a_turn` counts. So for a stop that follows a block, this is what that block
    said: the harness wrote it, and it is the one account of what the agent was told. `starts_a_turn`
    admits a list-shaped opener as readily as a string one, so the text is read from either shape."""
    opener = next((record for record in records_newest_first(transcript_path)
                   if starts_a_turn(record)), {})
    return text_of(opener.get("message", {}).get("content"))

def stop(hook, tokens, ceiling):
    """At most one close-out block per turn: a second one spends more context on the problem that
    IS too much context, and an agent that cannot run the close-out would be blocked forever.

    Which band the count falls in decides what the block says: every stop past the ceiling runs the
    same steps, and the band is the text they carry. [LAW:dataflow-not-control-flow] The band is
    read off the count and nothing else, so a session reset in place is back under both lines at
    its next stop with no allowance left over to clear.

    The one second block is the escalation. A finishing block tells the agent to keep working, so
    the turn it starts can run past the limit, and that turn ends in a stop the harness marks as
    following a block. Letting that stop through would break the promise the limit makes. So a stop
    past the limit is blocked once more if a finishing block opened its turn. The closing block that
    escalation sends then opens the next turn, and it is not a finishing block, so the chain ends."""
    limit = ceiling + GRACE
    band = CLOSING if tokens >= limit else FINISHING
    if closed_out(hook["transcript_path"]):
        return "closed-out", {"systemMessage": f"memento: the close-out ran at ~{tokens:,} "
                                               f"tokens, past the {ceiling:,} ceiling, so "
                                               f"the stop proceeds."}
    # [LAW:effects-at-boundaries] the turn opener is a second transcript read, so it is taken
    # only on the one path that consults it: a repeat stop, where the escalation decides whether
    # this block is the finishing band's promised second one rather than a spent close-out.
    if hook.get("stop_hook_active"):
        escalating = band is CLOSING and FINISHING_MARK in turn_opening(hook["transcript_path"])
        if not escalating:
            # The log keeps the two spent outcomes apart: spent-finish is a harmless continuation
            # under the grace, spent-block a hard-limit session that just used its one forced
            # close-out and, per the message, leaves the next session nothing.
            return f"spent-{band.label}", {"systemMessage": band.spent.format(
                tokens=tokens, ceiling=ceiling, limit=limit)}
    return band.label, {"decision": "block", "reason": band.reason.format(
        tokens=tokens, ceiling=ceiling, limit=limit, launcher=shlex.quote(LAUNCHER),
        exit_hint=EXIT_HINT)}

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
try:
    ceiling = resolve_ceiling(hook)
except SystemExit as unreadable:
    # [LAW:no-silent-failure] the config layer's failure arm is the process (ceiling_config
    # docstring), and Claude Code treats a stopped Stop hook as non-blocking - so the gate is now
    # off for this session. The exit is re-raised unchanged: the key stays rejected, loudly on
    # stderr. What is added is the one durable record that the gate stopped and why, since the log
    # is the only place a session running with no ceiling differs from one under it.
    log(hook, tokens, "unresolved", f"stopped: {unreadable.code}")
    raise

label, verdict = ("allow-under", None) if tokens < ceiling else stop(hook, tokens, ceiling)
log(hook, tokens, ceiling, label)
if verdict:
    print(json.dumps(verdict))
