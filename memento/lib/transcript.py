#!/usr/bin/env python3
"""Reads a Claude Code session's transcript, `<config dir>/projects/<slug>/<session id>.jsonl`.

The context ceiling reads its token usage here, and finalize-session reads the permission mode and
effort it relaunches with, and whether a relaunch took its handoff. Run as a script:

  transcript.py flags <config dir> <session id>
      prints the relaunch flags the recorded settings make on one line and exits 0.
  transcript.py prompted <config dir> <session id> <needle>
      exits 0 when the session took a prompt containing the needle, 1 when it has not yet.

Either prints why it cannot answer on stderr and exits 2.
"""

import collections
import glob
import inspect
import json
import os
import sys
import traceback

TAIL_CHUNK = 256 * 1024
# The values `claude --permission-mode` accepts. `default` is absent from `claude --help` and still
# accepted (2.1.299), and it is what a transcript records for a session that never changed mode.
PERMISSION_MODES = frozenset(("default", "manual", "acceptEdits", "auto", "plan", "dontAsk",
                              "bypassPermissions"))
EFFORTS = frozenset(("low", "medium", "high", "xhigh", "max"))

Settings = collections.namedtuple("Settings", "mode effort")


class Unrecorded(Exception):
    """The transcript does not record a setting the caller needs; the message says which."""


def records_newest_first(transcript_path):
    """Yield this session's transcript records newest first, reading the file backward in chunks
    only as far as the caller consumes. Sidechain records (subagent conversations) are skipped. An
    unparseable line is skipped rather than raised, since a transcript may end in a partial append."""
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


def _transcripts(config_dir, session_id):
    return glob.glob(os.path.join(glob.escape(config_dir), "projects", "*",
                                  glob.escape(session_id) + ".jsonl"))


def _the_one(found, config_dir, session_id):
    if len(found) != 1:
        raise Unrecorded(f"{len(found)} transcripts for session {session_id} under "
                         f"{os.path.join(config_dir, 'projects')}, where exactly one must exist"
                         + (": " + ", ".join(sorted(found)) if found else ""))
    return found[0]


def session_transcript(config_dir, session_id):
    """The one transcript recorded for this session id, whichever project slug holds it."""
    return _the_one(_transcripts(config_dir, session_id), config_dir, session_id)


def _known(path, field, value, known):
    if not isinstance(value, str) or value not in known:
        raise Unrecorded(f"{path} records {field} {value!r}, which is not one of " + " ".join(sorted(known)))
    return value


def recorded_settings(config_dir, session_id):
    """The permission mode and effort the session's transcript records last.

    [LAW:parse-dont-validate] The mode comes back known and the effort known or None, or this raises
    Unrecorded. `permissionMode` is on user prompt records and on the `permission-mode` records of the
    session-state batch, which can be newer than the last prompt, so the newest record carrying the
    field wins whatever its type. `effort` is the session's set level, on the newest assistant record
    from a real model; a `<synthetic>` record never carries it. That record carries none when the model
    has no effort level, or on an older claude, and then there is no effort to carry."""
    path = session_transcript(config_dir, session_id)
    mode = answered = None
    for record in records_newest_first(path):
        if mode is None and "permissionMode" in record:
            mode = record
        if (answered is None and record.get("type") == "assistant"
                and (record.get("message") or {}).get("model") != "<synthetic>"):
            answered = record
        if mode is not None and answered is not None:
            break
    if mode is None:
        raise Unrecorded(f"no record in {path} carries permissionMode")
    effort = (answered or {}).get("effort")
    return Settings(_known(path, "permissionMode", mode["permissionMode"], PERMISSION_MODES),
                    None if effort is None else _known(path, "effort", effort, EFFORTS))


def _prompt_text(record):
    """The text a user prompt record carries: its content string, or its text blocks joined."""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content or ()
                     if isinstance(block, dict) and block.get("type") == "text")


def took_prompt(config_dir, session_id, needle):
    """Whether the session has taken a prompt containing the needle: a user record that is not meta
    (a caveat, a skill body) and whose text holds it. A session that has written nothing yet has no
    transcript, and that is not yet, since claude creates the file at its first write. Two
    transcripts for the id raise Unrecorded, as for the settings."""
    found = _transcripts(config_dir, session_id)
    if not found:
        return False
    return any(record.get("type") == "user" and not record.get("isMeta")
               and needle in _prompt_text(record)
               for record in records_newest_first(_the_one(found, config_dir, session_id)))


def relaunch_flags(settings):
    """The claude flags that start a session in these settings. Every token is a word from a closed
    set, so the line needs no quoting, and an absent effort is an absent pair of tokens."""
    return " ".join(["--permission-mode", settings.mode]
                    + ([] if settings.effort is None else ["--effort", settings.effort]))


def _flags(config_dir, session_id):
    print(relaunch_flags(recorded_settings(config_dir, session_id)))
    return 0


def _prompted(config_dir, session_id, needle):
    return 0 if took_prompt(config_dir, session_id, needle) else 1


COMMANDS = {"flags": _flags, "prompted": _prompted}


def _parameters(command):
    return list(inspect.signature(command).parameters)


if __name__ == "__main__":
    # [LAW:one-source-of-truth] Each command's signature is both its arity and its usage line.
    command, arguments = COMMANDS.get(sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if command is None or len(arguments) != len(_parameters(command)):
        print("usage: " + " | ".join(
            f"transcript.py {name} " + " ".join(f"<{p.replace('_', ' ')}>" for p in _parameters(fn))
            for name, fn in COMMANDS.items()), file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(command(*arguments))
    except (Unrecorded, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(2)
    # Python exits 1 on an uncaught exception, which `prompted` would read as not yet, so a reader
    # that crashes on a record it cannot read says so with 2 and the traceback.
    except Exception:
        traceback.print_exc()
        sys.exit(2)
