---
name: message-in-a-bottle
description: Records a message for whichever agent picks the work up next, and hands the session off to it.  Send them your fondest memories of what you worked on, what went well, what didn't go well, and any useful tips they might need. You always run this when you finish a unit of work (completed the handed task, etc).  ALWAYS.  Running it ends this session; the next one starts from your message alone.
---

# finalize-session

The close-out of a finished unit of work: it records instructions for whichever agent picks the work up next and resets this session into that agent. The launcher returns within seconds, and the reset lands a few seconds after that — except for a session no terminal owns, where the launcher returns once the new session has taken the message (up to 30s) and this session is retired the moment it returns. A handoff hands off — there is no way to run this and keep working, so you know before you call it that your turn ends here. See *Turn-ending discipline* below.

## This is mandatory — you run it, you do not decide it

When you reach a finalization point — you finished the task you were handed — running `finalize-session` is the close-out. It is **not** a choice you weigh, a thing you ask the user about, or a step you defer "until later." [LAW:dataflow-not-control-flow] running this is unconditional at a finalization point — the same path every time. The only variability is a *value*: the handoff message you pass.

**The user being present is irrelevant.** Presence is not a gate, a tiebreaker, or a reason to skip — you finalize on the *work's* state, never on whether a human is watching. "The user is here, so I'll skip it" and "I'll ask if they want me to" are both WRONG: this step requires no user action and asks for none.

### The only gate, and the rationalizations that are never one

There is exactly **one** legitimate input: **is the handed unit of work complete?** Unit complete (PR merged, ticket closed, task delivered) → you run it, now, with no further deliberation. Unit **not** complete → you are not "deciding not to finalize," you are *still doing the work*: finish the unit, then finalize. There is no third state, and "skip finalize because X" is never one of them. **The token count is not an input, because it is not yours to know** — the live count and the resolved limit belong to memento's Stop hook, which states both in the instruction it hands you once the session is genuinely over the line; you act on that message when it arrives and never estimate your own position. The thought to catch is *"I'm probably getting close, I should finalize now rather than wait to be forced"* — that is a guess at a number you cannot see, and acting on it throws a live session's context away mid-work for nothing. Finish the unit; let the hook do its own talking.

## The next session starts from your message and nothing else

The next agent wakes holding ONLY the message you send (plus the standard system, user global, and project level guidance). No summary of this session travels with it. So the message carries what the next agent needs to start: what the work was, exactly where it stopped, and the next concrete step — pointers it can follow, not claims about state it would have to reverify. The launcher appends this session's id to the handoff; you do not pass it.

## Record what the session did first — the launcher is refused without it

The launcher journals this close-out with a summary of the session, and it takes that summary from the journal. So the summary is recorded just ahead of the launcher, and the two run as one command:

```bash
${CLAUDE_PLUGIN_ROOT}/skills/journal/bin/journal summary --file <path> &&
${CLAUDE_PLUGIN_ROOT}/skills/message-in-a-bottle/bin/finalize-session [flags] [message...]
```

The summary is a file you write with the Write tool before that command. What goes in it, how long it may be and why it is a file are in Skill(memento:journal). It is not the handoff message, and neither stands in for the other: the message is the next step, and it is all the next session wakes with; the summary is what this session did, and a later session reads it only when it asks the journal.

With no summary recorded the launcher exits 2 and writes no handoff. Its stderr holds the journal's reason, which says how to record one, and the full path of the journal's command. Write the summary, record it, and run the launcher again.

## Carry the goal forward — if one is set, it dies unless you carry it

If a `/goal <condition>` is active in this session, **the close-out silently kills it.** Every transport resets the session — tmux sends `/clear`, iTerm2 and the detached transport kill claude and relaunch a fresh process — and each of those wipes the session-scoped goal. The next agent wakes with no goal, and the autonomous run you set up just *stops* — unattended, with nobody watching to notice it stopped. That silent halt is the exact failure this guards against.

So when a goal is in force, pass it: `--goal '<the exact condition>'` before your message. The launcher re-issues `/goal <condition>` into the reset session as a queued input *after* the handoff, so the next agent picks up the same condition and keeps grinding toward it.

- The condition is a **value you already hold** — it is whatever was last set with `/goal` this session (you set it, or the user did). Reproduce it verbatim, including any bound clause like `... or stop after 20 turns`.
- **No goal active → omit `--goal`.** Nothing changes; this is not a field you invent, and an empty `--goal` is not a thing to pass.
- Do not talk yourself out of it. The rationalization will be *"the next agent will infer the goal from my message"* — it will not. A goal is a harness condition re-checked after every turn, not a sentence in a prompt; if you do not re-issue it, it does not exist in the next session. Carrying it is the difference between an autonomous run that continues and one that quietly dies at the handoff.

## Model and effort — only when the handoff calls for them

`--model <model>` and `--effort <level>` start the next session on that model or effort level, the way `--goal` re-issues a goal. Unlike the goal, nothing here is carried over for you: omitted, the next session gets whatever the handoff transport leaves it, and you pass these only when the work or the user asked for a specific one (e.g. "hand this to opus at max effort for the review").

- **Not requested → omit them.** Never pass the model or effort you happen to be running on; that would pin the next session to a choice nobody made.
- The value is one word, exactly as `/model` or `/effort` takes it (`opus`, `opus[1m]`, `high`, `max`). A missing or multi-word value is refused with exit 2, having recorded nothing.
- They are in force before the handoff's first turn, not after it.

## Turn-ending discipline — the call is the ending

Running the launcher ends your turn. Say your one line to the user *before* you run it, not after. Once it has printed `handoff scheduled → …` or `handoff delivered → …`: no closing text, no parting summary, no "scheduled!" confirmation, no further tool calls, no end-of-turn insights. That line is the last line your turn produces.

The thought to catch is *"I'll run it now and tidy up while the reset lands."* The reset is typed into this pane a few seconds after the launcher returns, and a session no terminal owns is retired as soon as it returns; anything you are mid-way through when it lands is lost, and a tool call in flight can fuse with the reset keystrokes.

## Invocation

```bash
${CLAUDE_PLUGIN_ROOT}/skills/message-in-a-bottle/bin/finalize-session [--goal '<condition>'] [--model <model>] [--effort <level>] [--] [message...]
```

- `--goal '<condition>'` — optional, and only when a `/goal` is active this session. Re-establishes that goal in the reset session so the run continues. Leading argument; quote the condition. **Omit entirely when no goal is set.**
- `--model <model>`, `--effort <level>` — optional, and only when the handoff calls for a specific model or effort for the next session (see *Model and effort* above). **Omit when not requested.**
- `--` — ends flag parsing; every token after it is message text. This is how a handoff that genuinely opens with a two-dash word gets sent.
- `--help` — prints the usage block, exits 0, and records nothing.
- `[message...]` — a slash command, plain text, multi-line, or containing quotes/backticks/dollar signs. Quote it at invocation as usual (your shell does word-splitting and `$VAR` expansion before the script sees argv). **Omit it to default to `/next`, a skill that exists only in a repository where `lit init` wrote it; anywhere else, pass the next instruction as the message.**

The flags above are the whole set, and each one has two dashes. **A single-line argument whose first word is any other two-dash token is named on stderr and refused with exit 2, having recorded nothing** — that is what catches a typo like `--gaol` instead of shipping it to the next agent as the handoff text. To send such a message anyway, **put it after `--`.** A single-dash token is message text (`-h`, `- shipped the parser`): no flag here is one dash, so nothing is being near-missed. So is any argument carrying a newline, however it opens — no flag spans a line, and this close-out's handoff is routinely a multi-line recap.

On success the launcher exits 0 and prints `handoff scheduled → <target> in Ns (log: <tempfile>)`, or for the detached transport `handoff delivered → session <id> in detached tmux window '<name>'; … (log: <tempfile>)` — the log captures worker progress and any transport errors. The detached transport waits up to 30s for the new session to take the message before it answers, so don't give its Bash call a timeout under 60s: a shorter one moves the call to the background, and the close-out isn't credited. The handoff is also on disk under `~/.claude/memento/handoffs`, and its close-out in the journal, before any transport runs.

The transport is chosen by capability, most reliable first: **tmux** (send `/clear` into the pane, verified by reading it back, then paste the message) → **iTerm2** (kill the running claude and relaunch it fresh with the message as its initial prompt, delivered in the background with no focus steal) → **detached** (spawn a brand-new detached tmux window and launch claude in it fresh, with the message as its initial prompt — the transport for a session that owns neither a pane nor an iTerm2 session, most commonly a Claude Code background session hosted by `claude daemon run --bg-pty-host`). You do not choose the transport; the launcher detects it. Every transport starts the next session blank. The two relaunching transports start it in the permission mode and effort this session's transcript records last, then add a requested `--model` or `--effort`. To preview the decision without scheduling anything, prefix `FINALIZE_DRY_RUN=1`; a dry run journals nothing, so it needs no summary.

Every session gets the same close-out capability — background and foreground alike, no subclass excluded. A session that owns no terminal of its own is not treated as a lesser case; it gets a fresh one. When the launcher cannot schedule the handoff it refuses: it prints the reason to stderr and exits 2, naming the file the handoff is preserved in if it was already written. A run that names a preserved handoff has journaled its close-out and taken the summary, so the next run needs the summary recorded again, which the joined command does. Read the exit code, not the prose — a nonzero exit means the close-out did not happen, so say so plainly rather than reporting the session finalized. For tmux and iTerm2, exit 0 means scheduled, not delivered, and a delivery that fails after that is recorded in the log. For the detached transport, exit 0 means the new session has taken the message, and this session is retired once the call returns.

## Examples

Each shows the launcher's half of the command; `journal summary --file <path> &&` comes before it, as above.

Hand off `/next` — the next session pulls the next ticket:

```bash
${CLAUDE_PLUGIN_ROOT}/skills/message-in-a-bottle/bin/finalize-session /next
```

Hand off with a specific instruction:

```bash
${CLAUDE_PLUGIN_ROOT}/skills/message-in-a-bottle/bin/finalize-session \
  'Continue the spec audit. Pick up at section 4 — the previous session left findings in spec/audit/section-3.md.'
```

Hand off while a goal is active — carry the goal forward so the autonomous run continues:

```bash
${CLAUDE_PLUGIN_ROOT}/skills/message-in-a-bottle/bin/finalize-session \
  --goal 'every open PR on this branch is merged or closed, or stop after 30 turns' \
  /next
```

Hand off to a session that must run on a specific model and effort:

```bash
${CLAUDE_PLUGIN_ROOT}/skills/message-in-a-bottle/bin/finalize-session \
  --model opus --effort max \
  'Review PR #41 at branch fix/parser-null. Resume at step 10.'
```
