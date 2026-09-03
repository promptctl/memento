---
name: next
description: Pull the next ticket — pointer only; the skill now ships with the `lit` binary and is written into the repo by `lit init`.
---

# Moved

This skill is no longer implemented here. It ships with the `lit` binary now: running
`lit init` (or `lit quickstart --refresh`) in the repository writes the current copy
to `.claude/skills/next/SKILL.md`, and from then on the project's own /next skill is
the one to use. Nothing in this file tells you how to pull a ticket, and
reconstructing the procedure from memory produces a second, drifting copy — don't.

Get it:

```
lit init          # or: lit quickstart --refresh
```

This requires a lit newer than 0.11.0 (`lit version` to check). If the installed lit
is older or absent, that comes first: `lit upgrade` upgrades an installed lit. Then
run the line above.

Installing is the default path, not the optional one. "I'll just pick a ticket myself
this once" is the rationalization to refuse: put the real skill in place first.

If lit genuinely cannot be installed or upgraded in this session, do exactly this:
tell the user in one line that /next now ships with lit (newer than 0.11.0) and could
not be set up, then proceed on your own judgment. Do not improvise a replacement
procedure and do not present it as this skill.
