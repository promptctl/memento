#!/usr/bin/env python3
"""Stop hook that blocks a session once its context passes the configured ceiling and asks it to close out.

It runs on Stop only, because the newest assistant record's usage describes the live context only after a turn has ended. Between the ceiling and the limit (the ceiling plus GRACE) the block asks the agent to finish its current unit and then close out; at or past the limit it asks for an immediate close-out. A stop that follows a finishing block and lands past the limit is blocked once more, and a stop after a close-out that ran is allowed. Every stop appends one line to the log and refreshes the session's status for cc-candybar.
"""

import collections
import json
import os
import re
import shlex
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The config grammar and layer order live in `ceiling_config`, which this hook and the `ceiling`
# command both import. The path is inserted before the import because the plugin is a directory,
# not an installed package.
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "lib"))
from ceiling_config import GRACE, anchored, in_force, session_directory  # noqa: E402
from ceiling_log import hook_payload, log, reason_of  # noqa: E402
# Every stop refreshes the session's status on cc-candybar's bar, refusals included. The ceiling is
# read from the layers again rather than passed in, so one reader serves every caller. A bar that
# cannot be updated does not stop the gate.
from candybar import published  # noqa: E402
from journal import SUMMARY_CAP  # noqa: E402
from transcript import records_newest_first  # noqa: E402

LAUNCHER = os.path.join(PLUGIN_ROOT, "skills", "message-in-a-bottle", "bin", "finalize-session")
# The journal's command, which records the summary the launcher's close-out takes.
JOURNAL = os.path.join(PLUGIN_ROOT, "skills", "journal", "bin", "journal")
# Matched by basename rather than absolute path, because an agent may reach the launcher by name or
# through a wrapper. The basename is a substring of the full path, so it covers both.
LAUNCHER_NAME = os.path.basename(LAUNCHER)
EVERY_PROMPT_COMPONENT = ("input_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens", "output_tokens")
# The rendered report the launcher prints when a handoff is scheduled (tmux, iTerm2) or delivered
# (detached), matched in a tool result to credit a close-out. Only the rendered line matches: the
# absolute log path a run fills in is required, while the prose and source text, which name the log
# by its variable, are not. finalize-session.test.py matches every success line the launcher prints
# against this.
RESET_MARKER = re.compile(r"handoff (?:scheduled|delivered) → .* \(log: /")
# What a block past the ceiling says depends on how far past it the session is. Up to the limit the
# agent finishes its current unit; from the limit on it closes out where it stands.
Band = collections.namedtuple("Band", "label reason spent")

# The phrase identifying a finishing block, matched against a turn opener when a later stop escalates.
# It is spliced into FINISHING's reason, so the phrase the agent sees is the phrase matched. Only
# finishing blocks carry it, because the escalation never needs to name a closing block.
FINISHING_MARK = "Finish the unit of work you are in the middle of"

# The close-out instructions both bands share: the two commands of a close-out, the summary and the
# handoff contract. Each band's reason is its own opening, this text, and one clause on what not to do.
CLOSE_OUT = """
    {journal} summary --file <path> &&
    {launcher} '<handoff message>'
The first command records what this session did, for the sessions after it to read back. Before you run it, use the Write tool to put what the session did, what came of it and why, in at most {cap:,} characters, in a file under /tmp, and give that file's path. The second records the handoff and resets this session into it; it is refused where the first has not run. Load Skill(memento:message-in-a-bottle) for the handoff contract. The handoff message is the ONLY thing the next session wakes up with, so it says what you were doing, exactly where you stopped, and the next concrete step. Pass it as one single-quoted argument, writing an apostrophe as '\\''; newlines inside the quotes are fine. """

FINISHING = Band("finish",
    "CONTEXT CEILING: this session is at ~{tokens:,} tokens, past the {ceiling:,} ceiling; {limit:,} is the hard limit. "
    + FINISHING_MARK
    + """ - the PR, the ticket, the task you were handed - and close out the moment it is done. Do not start another unit: the thought "the next ticket is small, I'll take it too" is starting one, and it belongs to the next session. If the unit is already done, or you are between units, close out now. If you are mid-unit and ended this turn only to wait on something (a background task, CI), end your turn again: that stop goes through, unless the session has reached {limit:,} by then, in which case this hook blocks it once more to make you close out wherever you stand. You do not track the count - this hook does. To close out, run:""" + CLOSE_OUT
    + "Do not move the ceiling to make room, and do not ask the user whether to finalize.",
    "memento: this session is past the {ceiling:,} ceiling at ~{tokens:,} tokens and stopped again "
    "without closing out, so the stop proceeds. It may finish the unit of work it is in; at "
    "{limit:,} it is made to close out.")

CLOSING = Band("block",
    """CONTEXT CEILING: this session is at ~{tokens:,} tokens, past the {ceiling:,} ceiling and the {limit:,} hard limit. Close it out now so the next session can pick the work back up. Run the close-out:""" + CLOSE_OUT
    + "Do not start new work, do not move the ceiling to make room, and do not ask the user whether to finalize.",
    "memento: context ceiling breached (~{tokens:,} > {ceiling:,}) and this session has spent its "
    "one forced close-out attempt, so the stop proceeds. If the close-out did not run, the next "
    "session starts with nothing.")

def resolve_ceiling(hook):
    """The ceiling in force for the payload's session, read from the config layers at this stop.
    Nothing is cached or written, so a change to the layers takes effect at the next stop. The
    project is anchored at the session's own directory rather than the shell's current one."""
    return in_force(session_directory(hook["session_id"]), anchored(hook["cwd"]))

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
    """Return the text of a `content` value, which is either a string or a list of blocks. Blocks
    without a `text` field contribute nothing."""
    if isinstance(content, str):
        return content
    return " ".join(block.get("text", "") for block in content or []
                    if isinstance(block, dict))

def result_text(block):
    """Return the text of a tool_result block's content."""
    return text_of(block.get("content"))

def launcher_ran(command):
    """Whether a Bash command invokes the launcher rather than printing its report. A real
    invocation cannot contain the rendered report line, because the log path in it is created
    while the command runs, so a command that carries the line is treated as printing it. The
    check is a substring test, so a handoff message quoting the line verbatim is not credited
    and the next stop blocks again."""
    return LAUNCHER_NAME in command and not RESET_MARKER.search(command)

def closed_out(transcript_path):
    """Whether a close-out ran in the turn now ending and reset the session. It needs three facts
    from the transcript: a successful tool result whose text matches RESET_MARKER, a Bash tool_use
    with the matching id, and a command that `launcher_ran` accepts as an invocation. Scanning
    stops at the start of the current turn, so a close-out from an earlier turn does not count.
    Records are read newest first, so a result is seen before the call it belongs to. It cannot
    detect a deliberate forgery, such as a script that prints the report line."""
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

def turn_opening(transcript_path):
    """Return the text of the record that began the turn now ending, or "" if it has no text. After
    a block, that record is the `Stop hook feedback:` message the harness wrote, so it shows what
    the agent was told."""
    opener = next((record for record in records_newest_first(transcript_path)
                   if starts_a_turn(record)), {})
    return text_of(opener.get("message", {}).get("content"))

def stop(hook, tokens, ceiling):
    """Return the log label and the hook verdict for a stop at or above the ceiling. The stop is
    allowed if a close-out ran this turn. Otherwise it is blocked with the reason for its band,
    except that a repeat stop (stop_hook_active) is allowed unless the escalation applies. The band
    is chosen from the count alone, so a session reset in place is back under both lines at its
    next stop. The escalation blocks a repeat stop past the limit once more when its turn opened
    with a finishing block; that closing block opens a turn which cannot escalate again."""
    limit = ceiling + GRACE
    band = CLOSING if tokens >= limit else FINISHING
    if closed_out(hook["transcript_path"]):
        return "closed-out", {"systemMessage": f"memento: the close-out ran at ~{tokens:,} "
                                               f"tokens, past the {ceiling:,} ceiling, so "
                                               f"the stop proceeds."}
    # The turn opener is a second transcript read, so it is taken only on a repeat stop, where the
    # escalation needs it to decide whether this block is the finishing band's promised second one.
    if hook.get("stop_hook_active"):
        escalating = band is CLOSING and FINISHING_MARK in turn_opening(hook["transcript_path"])
        if not escalating:
            # The log separates the two spent outcomes: spent-finish is a continuation under the grace,
            # and spent-block is a hard-limit session that has used its one forced close-out.
            return f"spent-{band.label}", {"systemMessage": band.spent.format(
                tokens=tokens, ceiling=ceiling, limit=limit)}
    return band.label, {"decision": "block", "reason": band.reason.format(
        tokens=tokens, ceiling=ceiling, limit=limit, launcher=shlex.quote(LAUNCHER),
        journal=shlex.quote(JOURNAL), cap=SUMMARY_CAP)}

# The ceiling depends on the payload's session and project, so it is resolved here rather than at
# import. Any failure before the gate decides is logged with the values established so far, then
# re-raised unchanged, so it stays loud. tokens and ceiling start as sentinels until each is computed.
# The log entry is the only place a gate-off session differs from an allowed one.
hook = None
tokens = "unknown"
ceiling = "unresolved"
try:
    hook = hook_payload("context ceiling", "Stop")
    tokens = context_tokens(hook["transcript_path"])
    ceiling = resolve_ceiling(hook)
    label, verdict = (("allow-under", None) if tokens < ceiling
                      else stop(hook, tokens, ceiling))
except (Exception, SystemExit) as unresolved:
    # `hook` is whatever parsed, or None/non-dict when the failure came before it was one - log
    # needs a dict to name a session.
    log(hook if isinstance(hook, dict) else {}, f"stopped: {reason_of(unresolved)}", tokens=tokens,
        ceiling=ceiling, **published(hook))
    raise
log(hook, label, tokens=tokens, ceiling=ceiling, **published(hook))
if verdict:
    print(json.dumps(verdict))
