---
name: ceiling
description: Move or lift the context ceiling for the session running right now — disable it, raise it by an amount, or pin it to a number, taking effect on the very next tool call. Use when the user says "disable the ceiling", "turn off the handoff", "stop gating this session", "give this session more room", "raise my ceiling", "I need another 100k", or when a session is about to be forced into a close-out it should not be forced into yet.
---

# Move this session's ceiling

The session layer is the most specific config layer, and the one a session can write
for itself. Writing it changes the ceiling in force on the very next tool call —
nothing to restart, reload or signal.

**Step 1 — write it.** Substitute one value from the table for `off`:

```bash
dir="${MEMENTO_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/promptctl}/sessions/$CLAUDE_CODE_SESSION_ID"
mkdir -p "$dir" && printf 'ceiling = %s\n' 'off' > "$dir/memento.conf"
```

**Step 2 — verify it took.** The hook logs the ceiling it resolved on every call, so
the next call after the write is the proof. Run it as its own call:

```bash
grep "session=${CLAUDE_CODE_SESSION_ID:0:8}" ~/.claude/memento/context-ceiling.log | tail -1
```

| Value | Resolves to |
|---|---|
| `off` | `ceiling=inf` — no ceiling for this session |
| `+100_000` | 100k above whatever the layers beneath resolved to, without needing to know that number |
| `400_000` | exactly that, ignoring the layers beneath |

Hand the session back to the normal ceiling by deleting the file:

```bash
rm -f "${MEMENTO_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/promptctl}/sessions/$CLAUDE_CODE_SESSION_ID/memento.conf"
```

## Why step 2 is not optional

The key was `context_ceiling` before memento 0.5.0 and `ceiling` after. Writing the
name this session's hook does not accept does not leave the old ceiling standing — it
stops the hook with an error, which Claude Code treats as non-blocking, so the gate
silently stops running for every session on this machine.

The `printf` exits 0 either way. So the failure mode of guessing wrong is not "my
ceiling didn't move", it is "the ceiling is off everywhere and nothing said so", and
step 2 is the only thing that tells those apart:

    WRONG: "Wrote ceiling = off to the session config. The ceiling is disabled."
    RIGHT: "ceiling=inf on the last hook call (02:14:07). Disabled and confirmed."

If the log line is unchanged, read the error the hook printed — it names the key it
accepts, as `It reads: ceiling.` — rewrite with that key and verify again.

## If the session is already past the ceiling

The gate denies every Bash call that is not `git` or the close-out launcher, and every
Skill call that is not the close-out — including this one. A breached session cannot
raise its own ceiling; the escape hatch is for a session that sees the wall coming.

Say so, and give the user the command to run in their own shell with the session id
filled in. It lands on this session's next tool call:

```bash
dir="${XDG_CONFIG_HOME:-$HOME/.config}/promptctl/sessions/<this-session-id>"
mkdir -p "$dir" && printf 'ceiling = off\n' > "$dir/memento.conf"
```

One denial above the ceiling is the answer. Retrying burns the context the ceiling is
complaining about.
