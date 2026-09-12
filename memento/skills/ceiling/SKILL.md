---
name: ceiling
description: Move or lift the context ceiling — for this project or for this session alone — taking effect in the session that runs it, the next time the ceiling is checked, when this turn ends. Use when the user says "disable the ceiling", "turn off the handoff", "give this session more room", "raise my ceiling", "I need another 100k", "stop gating this session", "set the ceiling for this project", "this repo needs more headroom", "stop gating this project", or when a session is about to be forced into a close-out it should not be forced into yet.
---

# Move the context ceiling

One command does the whole job: it resolves the number, writes every layer the scope
names, reads each file back, and ends by printing the ceiling in force. Nothing to
hand-write, nothing to go and confirm on a later turn.

```bash
${CLAUDE_PLUGIN_ROOT}/skills/ceiling/bin/ceiling show
${CLAUDE_PLUGIN_ROOT}/skills/ceiling/bin/ceiling set <session|project> <off|N|+N|-N>
${CLAUDE_PLUGIN_ROOT}/skills/ceiling/bin/ceiling clear <session|project>
```

The scope is a required word, not a flag — there are no flags at all. Leave it out or
misspell it and the command exits 2 having written nothing.

| Value | What it sets |
|---|---|
| `off` | no ceiling at all — the gate stops firing, and the report reads `no ceiling` |
| `+100_000`, `-50_000` | that much more or less than the ceiling beneath, without having to look that number up |
| `400_000` | exactly that, whatever the layers beneath say |

Underscores in numbers are fine. A unit suffix like `100k` is not, and is refused.

A new ceiling is in force the next time the hook checks, which is when this turn ends.
There is nothing to restart, reload or signal, and this works from a session already
past its ceiling — which is the usual reason to be here.

## Pick the scope from what the user said

`session` — "this session", "right now", "I need another 100k to finish this". Writes
this session's own layer and nothing else; it outlives nothing.

`project` — "this project", "this repo", "always here", "stop gating this project".
Persists, and leaves a file in the checkout.

Their words settle it. Don't ask which they meant.

Reach for `show` when you need to know where the ceiling stands before moving it, or
when the user is only asking.

## `set project` writes two files, and that is the feature

It writes `.promptctl/memento.conf` at the repository root — or rewrites whichever
project config is already in force at or above the working directory — **and** this
session's own layer.

That second write is what makes the change reach the session that asked for it. Each
session's user and project layers are frozen at its first stop, into a record the hook
keeps; so the project file alone moves the ceiling for every session *after* this one
and leaves this one exactly where it was. Both files receive the same resolved absolute
number, so they cannot drift into stating different ceilings. [LAW:one-source-of-truth]

Two things to carry into what you tell the user:

- **The project file lands in their checkout and is not gitignored.** Name the file and
  say it is untracked. Committing it is their call, not yours.
- **`set project` needs a git repository**, which is how the root a new project config
  lands at is found. Outside one it exits 1 and says so; `set session` still works there,
  and so does `clear project` — removing files that already exist asks git nothing.

## Verification is the last lines of the output already in front of you

Every command — `show` and `clear` included — ends with the ceiling in force and the
layers behind it:

```
this session        350,000 tokens
  shared at start   (not recorded) …/sessions/<id>/shared-at-start.conf
  session layer     350000         …/sessions/<id>/memento.conf
a new session here  350,000 tokens
  project layer     350000         /repo/.promptctl/memento.conf
  user layer        (unset)        …/memento.conf
```

`this session` is the number this session is gated on; `a new session here` is what the
next session in this project starts with. Those two lines are the confirmation — the
command renders only values the hook's own reader accepts and reads each file back
after writing it, so exit 0 means the files say what it printed.
[LAW:verifiable-goals]

A nonzero exit reports on a file, not on your write: the `wrote` lines print first, and
every one that printed stands — read the lines, not your intention.
[LAW:no-silent-failure] `1` is a config file or a resolved ceiling that is unusable,
named with the file and line to go fix — a `-900_000` that lands below zero, a value some
file holds that does not parse — and that file can be a layer this command never touched,
so `set session 400_000` can write the session layer and exit 1 over a malformed user
layer. Rightly: a layer the hook's reader cannot parse stops the Stop hook, Claude Code
treats a dead Stop hook as non-blocking, and the gate is off for every session reading
that file. Report both — what was written, and which file to go fix. `2` is a wrong
invocation, and nothing was written.

The temptation is the easy one: you typed `+100_000`, it exited 0, and you report
350,000 from arithmetic you did in your head. Read the line instead — a signed move is
an offset against a layer you deliberately did not look up, so the total is the
command's to state, not yours.

    WRONG: "Wrote the project ceiling. The ceiling is now 350,000."
    RIGHT: "Project ceiling set to 350,000 tokens, in force for this session too
            (`.promptctl/memento.conf` at the repo root — untracked, yours to commit
            or not)."

## `clear` hands a layer back to the one beneath it

`clear session` returns this session to the shared layers it started under.

`clear project` removes the project layer and this session's: later sessions fall
through to the layer beneath the project, and this session returns to the ceiling it
*started* under — the frozen record still stands, so it does not fall back to whatever
the files say now. Where no project config stood there was nothing to remove, so only the
session layer's line prints and `project layer     (none)` in the report is what says so.

Either way `clear` prints what each file held before removing it — as `set` prints
`(replacing 900000)`, the only record of a session ceiling that `set project` overwrote —
so a ceiling someone meant to keep is recoverable from the output rather than from whoever
remembers it.
