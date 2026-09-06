# memento

A Claude Code marketplace with one plugin in it. It is about one problem: an agent
session has a beginning, a middle, and an end, and the ends are where work gets lost —
a ticket picked up with no context, a PR review half-addressed, a session that hits the
context limit and forgets what it was doing.

`memento` gives you three skills you invoke by hand: pull the next ticket, work a PR
review to clean, write a handoff for the next session. It also ships one hook, which
takes that last skill and makes it mandatory — past a token ceiling, a session cannot
end its turn, or call any other tool, until it has written the handoff.

## Install

Inside a Claude Code session:

```
/plugin marketplace add promptctl/memento
/plugin install memento@memento
```

The same thing from a shell:

```bash
claude plugin marketplace add promptctl/memento
claude plugin install memento@memento
```

`memento@memento` reads as *plugin `memento` from marketplace `memento`* — the
marketplace and its plugin share a name. It installs to user scope by default;
`claude plugin install --scope project` (or `local`) puts it elsewhere.

**Upgrading from 0.3.0 or earlier.** This repo used to ship a second plugin,
`auto-bottle`, which carried the context-ceiling hook and exposed the same close-out
skill file under its own namespace. As of 0.4.0 it is gone. Uninstall it and update
`memento`, which now carries the hook:

```bash
claude plugin uninstall auto-bottle@memento
claude plugin update memento@memento
```

Nothing is lost in the move. The ceiling and the close-out both live in `memento` now,
and the close-out has exactly one name: `memento:message-in-a-bottle`.

## Skills

**`next`** — a pointer, not an implementation. The procedure for pulling a ticket ships
with the `lit` binary: `lit init` (or `lit quickstart --refresh`) writes the current copy
to the repository's own `.claude/skills/next/SKILL.md`, and from then on that is the one
to use. It needs a lit newer than 0.11.0. The skill here exists to say so, and to stop an
agent reconstructing the procedure from memory into a second copy that drifts.

**`address-pr-reviews`** — works a PR's review feedback to clean. Each round: fetch
every open finding, post a plan on each thread, implement, push (which re-runs the
reviewer), resolve the threads that are genuinely fixed, dismiss the reviewer's now-stale
change request. Repeat until a fetch returns nothing. Disagreeing is a first-class
outcome — push back with reasoning rather than complying with a wrong finding.

The review backend is pluggable. `memento/skills/address-pr-reviews/provider.json` names
the active provider (the `PR_REVIEW_PROVIDER` environment variable overrides it), and each
provider is a Python module declaring a `CAPABILITIES` dict that says which operations
it supports. Three ship today:

| Provider | What it is | Notes |
| --- | --- | --- |
| `action` (default) | the `brandon-fryslie/coding-agent-review` GitHub Action | posts a blocking review; findings are resolvable threads |
| `adversarial` | a headless Claude agent run as a hostile reviewer | posts COMMENT reviews, so there is nothing to dismiss |
| `local` | stub for a locally-running agent | raises `NotImplementedError` — not usable yet |

The contract for writing a fourth is in
`memento/skills/address-pr-reviews/PROVIDER_CONTRACT.md`.

**`message-in-a-bottle`** — writes the message a future session wakes up with. You run
it at the end of a unit of work (PR merged, ticket closed, task delivered) or when the
context is running out. It calls
`memento/skills/message-in-a-bottle/bin/finalize-session`, which schedules a delayed
handoff into your own session: the session resets, and the message you wrote arrives as
the next agent's opening prompt.

```bash
finalize-session [--goal '<condition>'] [--reset clear|compact] [message...]
```

With no message it hands off `/next`. `--reset` decides whether the next session starts
blank or with a compacted summary — honoured on the tmux transport only, since the
other two transports launch a fresh process and are blank by construction. `--goal`
re-issues an active `/goal` condition into the reset session, which otherwise dies
silently at the handoff and stops an unattended run.

The launcher picks its transport by capability: reset the tmux pane in place, else kill
and relaunch the iTerm2 session, else spawn a fresh detached tmux window. Prefix
`FINALIZE_DRY_RUN=1` to see which one it would choose without scheduling anything.

## The context ceiling

What makes the close-out fire without being asked is
`memento/hooks/scripts/context-ceiling.py`, registered on both `Stop` and `PreToolUse`.

On either event the hook reads the transcript for the most recent assistant message's
token usage (all four fields — input, output, cache creation, cache read — because that
is what the next request carries) and compares it to the ceiling. Under the ceiling, the
hook says nothing.

The ceiling is 250,000 tokens by default, and four layers can move it. From the least
specific to the most: `~/.config/promptctl/memento.conf`, then the nearest
`.promptctl/memento.conf` at or above the project directory, then
`~/.config/promptctl/sessions/<session-id>/memento.conf`, then the
`MEMENTO_CONTEXT_CEILING` environment variable. Same filename everywhere, so a second
setting is one more key rather than one more file, one more lookup and one more
precedence chain. The home-rooted layers follow `XDG_CONFIG_HOME` where it is set, and
`MEMENTO_CONFIG_HOME` moves them outright.

Each file is `key = value` lines, `#` starts a comment, and `ceiling` is the only key
so far:

```
# this repo runs long
ceiling = 350_000
```

It takes a count, a signed adjustment, or one of `off`, `none`, `never`, `disabled` in
place of a number, which turns the ceiling off entirely. An adjustment applies to
whatever the layers beneath it resolved to, which is what lets one session delay its own
handoff without touching — or knowing — the number the project pinned:

```
ceiling = +100_000
```

A session can create that layer for itself. The `<session-id>` in its path comes from
`CLAUDE_CODE_SESSION_ID`, so one command gives the session running right now more room:

```
dir="${MEMENTO_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/promptctl}/sessions/$CLAUDE_CODE_SESSION_ID"
mkdir -p "$dir" && echo 'ceiling = +100_000' > "$dir/memento.conf"
```

Because the value is signed it lands on top of what the project pinned rather than
replacing it — a project at 250,000 resolves to 350,000 — and it takes effect on the very
next hook invocation, with nothing to restart, reload or signal. A session raises its
ceiling before it breaches it, not after: past the ceiling the `PreToolUse` gate denies
that command along with every other Bash call that is not `git` or the close-out
launcher.

The project layer is found by walking up, so a subdirectory or a worktree inherits the
repo above it, and it is anchored at `CLAUDE_PROJECT_DIR` where Claude Code sets it, so
the ceiling cannot change because something ran `cd`. Anything else — a typo, a unit
suffix, a misspelled key, a key set twice in one file, an adjustment resolving below
zero — stops the hook with an error naming where the setting came from: the file and the
line for a config file, the variable for `MEMENTO_CONTEXT_CEILING`. It never falls back
quietly to the default, on the grounds that a ceiling you believe you moved and did not
is worse than no ceiling. Every decision the hook makes is appended to
`~/.claude/memento/context-ceiling.log` (`MEMENTO_CEILING_LOG`), which is the only place
you can tell an allow apart from a hook that never ran.

Over the ceiling on `Stop`, the hook returns `{"decision": "block"}`. Claude Code refuses
the stop and hands the hook's `reason` back to the agent as its next instruction: commit
or push everything outstanding first, then run the `finalize-session` launcher with a
handoff message.

It forces this **once per session**. If the session stops again, the hook sees
`stop_hook_active` and lets the stop proceed, printing a visible system message saying
the one forced attempt was spent. A second block would spend more context on the problem
that *is* too much context.

`Stop` alone is not enough, because it only has teeth in a session that stops — and an
autonomous session never stops, which is exactly the session the ceiling exists to
catch. So `PreToolUse` enforces the same ceiling inside the tool loop, where it cannot
be avoided. Above the ceiling it is default-deny: the tool call is not run, and the
agent gets the close-out instruction back as the denial reason. Only three things
are permitted through:

- the `memento:message-in-a-bottle` skill, which is the close-out itself;
- a Bash call to the `finalize-session` launcher, matched by resolved path rather than by
  name, so something else wearing that name is still not the close-out;
- `git status`, `diff`, `log`, `show`, `rev-parse`, `add`, `commit`, and `push`, which is
  enough to see the tree and get outstanding work committed before the handoff.

A Bash command the hook cannot parse is denied too; if it was reaching for the launcher,
the denial says so and explains how to requote it. Denial withholds tools and never the
exit, so it cannot wedge a session — which is why `PreToolUse` needs no spent-attempt
valve and keeps no state.

## Repo layout

One plugin, one directory, and every file is a real file:

```
.claude-plugin/marketplace.json
memento/.claude-plugin/plugin.json
memento/CHANGELOG.md
memento/hooks/hooks.json
memento/hooks/scripts/context-ceiling.py
memento/skills/next/
memento/skills/address-pr-reviews/
memento/skills/message-in-a-bottle/
```

Nothing in this repo is a symlink, and no skill exists twice. To change the text of
`message-in-a-bottle`, edit `memento/skills/message-in-a-bottle/SKILL.md` — the file the
plugin ships is the file you edit, and `claude plugin validate ./memento` checks that
same content.

## Releases

The plugin carries its version in `memento/.claude-plugin/plugin.json`. The marketplace
entry deliberately carries **no** version field, so there is no second declaration that
could disagree with the manifest.

Tags are named `<plugin>--v<version>`, which with a single plugin means one tag per
release. `claude plugin tag memento --push` creates it and publishes
`memento/CHANGELOG.md`'s newest section as the release notes.

Releases follow the org-wide procedure in
[promptctl/.github's RELEASING.md](https://github.com/promptctl/.github/blob/master/RELEASING.md).
This repo's `.github/workflows/release.yml` only says when that procedure runs; the how lives there.

## License

MIT. Copyright (c) 2026 Brandon Fryslie. See [LICENSE](LICENSE).
