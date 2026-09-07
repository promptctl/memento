---
name: ceiling
description: Move or lift the context ceiling for the session running right now — disable it, raise it by an amount, or pin it to a number, taking effect on the very next tool call. Use when the user says "disable the ceiling", "turn off the handoff", "stop gating this session", "give this session more room", "raise my ceiling", "I need another 100k", or when a session is about to be forced into a close-out it should not be forced into yet.
---

# Move this session's ceiling

The ceiling reads four config layers on every hook invocation, and the session layer is
the most specific one a session can write for itself. Writing it changes the ceiling in
force on the very next tool call — nothing to restart, reload or signal.

The file is one line, at a path derived from `CLAUDE_CODE_SESSION_ID`:

```
${MEMENTO_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/promptctl}/sessions/$CLAUDE_CODE_SESSION_ID/memento.conf
```

## Do it in two steps. Both of them.

**Step 1 — write it.** Substitute one value from the table below for `off`:

```bash
dir="${MEMENTO_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/promptctl}/sessions/$CLAUDE_CODE_SESSION_ID"
mkdir -p "$dir" && printf 'ceiling = %s\n' 'off' > "$dir/memento.conf"
```

**Step 2 — verify it took.** The hook logs the ceiling it resolved on every call, so the
next tool call after the write is the proof. Run this as its own call:

```bash
grep "session=${CLAUDE_CODE_SESSION_ID:0:8}" ~/.claude/memento/context-ceiling.log | tail -1
```

It must show `ceiling=inf` for `off`, or the number you asked for. That line is the
acceptance criterion — the live hook's own account of what it resolved.

| Value | Effect |
|---|---|
| `off` (also `none`, `never`, `disabled`) | No ceiling at all for this session. Resolves to `ceiling=inf`. |
| `+100_000` | Adds to whatever the layers beneath resolved to, so a project pinned at 250,000 becomes 350,000. Signed, so it does not need to know that number. |
| `-50_000` | Subtracts the same way. |
| `400_000` | Pins this session to exactly that count, ignoring the layers beneath. |

To hand the session back to the normal ceiling, delete the file:

```bash
rm -f "${MEMENTO_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/promptctl}/sessions/$CLAUDE_CODE_SESSION_ID/memento.conf"
```

## Step 2 is not optional, and here is what it catches

The key was named `context_ceiling` in memento 0.4.0 and earlier, and `ceiling` from
0.5.0 on. Writing the name this session's hook does not accept does **not** leave the old
ceiling in place — it stops the hook with an error before it resolves anything, and
Claude Code treats that as a non-blocking error, so the gate silently stops running for
every session on this machine.

So the failure mode of guessing wrong is not "my ceiling didn't move." It is "the ceiling
is now off everywhere and nothing said so." Step 2 distinguishes those two outcomes and
nothing else does.

If step 2 shows no new line, or the ceiling is unchanged, read the error the hook
printed — it names the key it does accept, in the form `It reads: ceiling.` — rewrite the
file with that key, and verify again.

Do NOT report the ceiling as moved on the strength of the write succeeding. A `printf`
into a file that the hook then refuses to parse exits 0 and looks exactly like success:

    WRONG: "Wrote ceiling = off to the session config. The ceiling is disabled."
    RIGHT: "ceiling=inf on the last hook call (log line 02:14:07). Disabled and confirmed."

## If the session is already past the ceiling

Above the ceiling the `PreToolUse` gate denies every Bash call that is not `git` or the
close-out launcher, and every Skill call that is not the close-out — including this one.
A session that has already breached cannot raise its own ceiling. That is by design: the
escape hatch is for a session that sees the wall coming, not one already against it.

When that happens, say so and give the user the command to run in their own shell, with
the session id already filled in — they can run it with a `!` prefix in the prompt or in
any other terminal, and it takes effect on this session's next tool call:

```bash
dir="${XDG_CONFIG_HOME:-$HOME/.config}/promptctl/sessions/<this-session-id>"
mkdir -p "$dir" && printf 'ceiling = off\n' > "$dir/memento.conf"
```

Do not spend denied tool calls discovering this a second time. One denial above the
ceiling is the answer, and each retry burns the context the ceiling is complaining about.
