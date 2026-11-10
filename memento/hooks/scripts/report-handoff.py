#!/usr/bin/env python3
# A relaunched session starts in the permission mode and effort Claude Code reports here, so for a command that names finalize-session they are written to ~/.claude/memento/reports/<session id>, which the launcher reads and removes. The write goes through a rename, so the launcher never reads half a report.
import json, os, sys
payload = json.load(sys.stdin)
if "finalize-session" in payload["tool_input"]["command"]:
    reports = os.path.join(os.path.expanduser("~"), ".claude", "memento", "reports")
    os.makedirs(reports, exist_ok=True)
    report = os.path.join(reports, payload["session_id"])
    with open(report + ".partial", "w") as staged:
        staged.write(f"{payload.get('permission_mode', '')}\n{(payload.get('effort') or {}).get('level', '')}\n")
    os.replace(report + ".partial", report)
