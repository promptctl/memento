#!/usr/bin/env python3
# A session that has handed off acts on nothing: once its successor took the message the work is the successor's, so every tool call the old context makes is denied while its record stands (lib/handed_off.py), whatever turn makes it, a goal's Stop-hook turns included.
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
import handed_off
session_id = json.load(sys.stdin)["session_id"]
successor = handed_off.reason(session_id)
if successor is not None: print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": f"This session has handed off: {successor.strip()} It holds the work now, and this session is retired or being retired, so do nothing more and end your turn. If you, the user, reopened this session to work in it again, remove {handed_off.path(session_id)} first."}}))
