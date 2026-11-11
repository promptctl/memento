#!/usr/bin/env python3
# A session that has handed off acts on nothing: once its successor took the message the work is the successor's, so while its record stands (lib/handed_off.py) the old context's first tool call is denied and its turn is ended, which pauses a /goal rather than letting its Stop hook start another turn.
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
import handed_off
session_id = json.load(sys.stdin)["session_id"]
try:
    successor = handed_off.reason(session_id)
except OSError as error:
    # [LAW:no-silent-failure] A hook that crashes lets the call through, so a record that stands but cannot be read fences as one that can.
    successor = f"its record could not be read, so its successor is unknown ({error})."
if successor is not None:
    # The deny keeps the call from running, and `continue: false` ends the turn, showing the reason to the user, who is the one who can lift the fence.
    reason = f"This session has handed off: {successor.strip()} Its successor holds the work, so this session is stopped while it is retired. To work in this session again, remove {handed_off.path(session_id)} first."
    print(json.dumps({"continue": False, "stopReason": reason, "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
