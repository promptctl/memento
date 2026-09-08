---
name: ceiling
description: Move or lift the context ceiling for the session running right now — disable it, raise it by an amount, or pin it to a number, taking effect the next time the ceiling is checked, when this turn ends. Use when the user says "disable the ceiling", "turn off the handoff", "stop gating this session", "give this session more room", "raise my ceiling", "I need another 100k", or when a session is about to be forced into a close-out it should not be forced into yet.
---

# Move this session's ceiling

The session layer is the most specific config layer, and the one a session can write
for itself. Writing it changes the ceiling in force the next time the ceiling is
checked, when this turn ends — nothing to restart, reload or signal.

**Step 1 — write it.** Substitute one value from the table for `off`:

```bash
dir="${MEMENTO_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/promptctl}/sessions/$CLAUDE_CODE_SESSION_ID"
mkdir -p "$dir" && printf 'ceiling = %s\n' 'off' > "$dir/memento.conf"
```

**Step 2 — verify it took.** The hook runs when a turn ends, so it writes one line per
turn: the line that proves your write took is written as this turn ends, and is there
to read on the next one. An unchanged log inside the turn that did the write is the
expected sight, not a failed write — grep it on your next turn:

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

`ceiling` is the only key this hook accepts — it was `context_ceiling` before memento
0.5.0. Writing a key it does not accept does not leave the old ceiling standing: it
stops the hook with an error, which Claude Code treats as non-blocking, so the gate
silently stops running for every session on this machine.

The `printf` exits 0 either way. So the failure mode of guessing wrong is not "my
ceiling didn't move", it is "the ceiling is off everywhere and nothing said so", and
step 2 is the only thing that tells those apart:

    WRONG: "Wrote ceiling = off to the session config. The ceiling is disabled."
    RIGHT: "ceiling=inf on the last hook call (02:14:07). Disabled and confirmed."

If a turn has ended since the write and the newest line still predates it, read the
error the hook printed — it names the key it accepts, as `It reads: ceiling.` —
rewrite with that key and verify again.
