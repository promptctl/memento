# memento

A Claude Code plugin marketplace with two plugins in it. Both are about the same
problem: an agent session has a beginning, a middle, and an end, and the ends are
where work gets lost — a ticket picked up with no context, a PR review half-addressed,
a session that hits the context limit and forgets what it was doing.

- **`memento`** gives you three skills you invoke by hand: pull the next ticket,
  work a PR review to clean, write a handoff for the next session. No hooks. Nothing
  fires on its own.
- **`auto-bottle`** takes the handoff skill and makes it mandatory, with a hook that
  refuses to let a session past a token ceiling end its turn, or call any other tool,
  until it has written one.

Install `memento` if you want the skills available. Also install `auto-bottle` if you
want the close-out enforced instead of remembered.

## Install

Inside a Claude Code session:

```
/plugin marketplace add promptctl/memento
/plugin install memento@memento
/plugin install auto-bottle@memento
```

The same thing from a shell:

```bash
claude plugin marketplace add promptctl/memento
claude plugin install memento@memento
claude plugin install auto-bottle@memento
```

`memento@memento` reads as *plugin `memento` from marketplace `memento`* — the
marketplace and one of its plugins share a name. Both install to user scope by
default; `claude plugin install --scope project` (or `local`) puts it elsewhere.

## The `memento` plugin

Three skills, version 0.1.0, no hooks.

**`next`** — picks up the next ready ticket and starts work. It assumes the `lit`
issue tracker is on your PATH, and begins by running `lit quickstart`. Before touching
the backlog it resolves what is already in flight: uncommitted changes get committed,
stashed, or discarded on their merits; an open PR on the current branch becomes the
ticket, worked through `address-pr-reviews`. Only then does it pull from `lit ready` —
orphaned tickets first, otherwise the top of the queue. It is written to investigate
before asking, and says so bluntly.

**`address-pr-reviews`** — works a PR's review feedback to clean. Each round: fetch
every open finding, post a plan on each thread, implement, push (which re-runs the
reviewer), resolve the threads that are genuinely fixed, dismiss the reviewer's now-stale
change request. Repeat until a fetch returns nothing. Disagreeing is a first-class
outcome — push back with reasoning rather than complying with a wrong finding.

The review backend is pluggable. `skills/address-pr-reviews/provider.json` names the
active provider (the `PR_REVIEW_PROVIDER` environment variable overrides it), and each
provider is a Python module declaring a `CAPABILITIES` dict that says which operations
it supports. Three ship today:

| Provider | What it is | Notes |
| --- | --- | --- |
| `action` (default) | the `brandon-fryslie/coding-agent-review` GitHub Action | posts a blocking review; findings are resolvable threads |
| `adversarial` | a headless Claude agent run as a hostile reviewer | posts COMMENT reviews, so there is nothing to dismiss |
| `local` | stub for a locally-running agent | raises `NotImplementedError` — not usable yet |

The contract for writing a fourth is in `skills/address-pr-reviews/PROVIDER_CONTRACT.md`.

**`message-in-a-bottle`** — writes the message a future session wakes up with. You run
it at the end of a unit of work (PR merged, ticket closed, task delivered) or when the
context is running out. It calls `skills/message-in-a-bottle/bin/finalize-session`,
which schedules a delayed handoff into your own session: the session resets, and the
message you wrote arrives as the next agent's opening prompt.

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

## The `auto-bottle` plugin

Version 0.1.0. It exposes the same `message-in-a-bottle` skill — the identical file,
not a second copy — and adds the thing that makes it fire without being asked:
`hooks/scripts/context-ceiling.py`, registered on both `Stop` and `PreToolUse`.

On either event the hook reads the transcript for the most recent assistant message's
token usage (all four fields — input, output, cache creation, cache read — because that
is what the next request carries) and compares it to the ceiling. Under the ceiling, the
hook says nothing.

The ceiling is 250,000 tokens by default. `MEMENTO_CONTEXT_CEILING` overrides that;
failing that, the hook reads `~/.claude/memento/context-ceiling` (put the file elsewhere
with `MEMENTO_CEILING_FILE`). Either source may hold `off`, `none`, `never`, or
`disabled` in place of a number, which turns the ceiling off entirely. Anything else — a
typo, a unit suffix — stops the hook with an error rather than quietly falling back to
the default, on the grounds that a ceiling you believe you moved and did not is worse
than no ceiling. Every decision the hook makes is appended to
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

- the `message-in-a-bottle` skill, under either namespace — `auto-bottle:message-in-a-bottle`
  or `memento:message-in-a-bottle` name the one skill file, and denying one of them would
  block the close-out for whoever invoked it by the other name;
- a Bash call to the `finalize-session` launcher, matched by resolved path rather than by
  name, so something else wearing that name is still not the close-out;
- `git status`, `diff`, `log`, `show`, `rev-parse`, `add`, `commit`, and `push`, which is
  enough to see the tree and get outstanding work committed before the handoff.

A Bash command the hook cannot parse is denied too; if it was reaching for the launcher,
the denial says so and explains how to requote it. Denial withholds tools and never the
exit, so it cannot wedge a session — which is why `PreToolUse` needs no spent-attempt
valve and keeps no state.

## Repo layout

Every skill exists exactly once on disk, at the repo root:

```
.claude-plugin/marketplace.json
skills/next/                    real content
skills/address-pr-reviews/      real content
skills/message-in-a-bottle/     real content
hooks/hooks.json                real content
hooks/scripts/context-ceiling.py
memento/.claude-plugin/plugin.json
memento/skills/next                    -> ../../skills/next
memento/skills/address-pr-reviews      -> ../../skills/address-pr-reviews
memento/skills/message-in-a-bottle     -> ../../skills/message-in-a-bottle
auto-bottle/.claude-plugin/plugin.json
auto-bottle/skills/message-in-a-bottle -> ../../skills/message-in-a-bottle
auto-bottle/hooks                      -> ../hooks
```

The two plugin directories hold no content of their own. Each is a manifest plus a set
of symlinks declaring which of the shared skills that plugin exposes.
`message-in-a-bottle` is pointed at by both, from the one file. At install time Claude
Code follows the symlinks and copies the real content into each plugin's own cache
directory, so an installed plugin is self-contained and there is no second copy in this
repo to drift out of sync.

**If you are editing a skill, edit `skills/<name>/`.** To change the text of
`message-in-a-bottle`, that means `skills/message-in-a-bottle/SKILL.md` — never
`memento/skills/message-in-a-bottle/` or `auto-bottle/skills/message-in-a-bottle/`,
which are links to the same file and exist only to say which plugin exposes it.

One consequence worth knowing: `claude plugin validate ./memento` does not follow
symlinks and will warn that it skipped them. Validate `./skills/<name>` directly to
check the real content.

## Releases

Each plugin carries its own version in its own `.claude-plugin/plugin.json` and releases
independently. The marketplace entries deliberately carry **no** version field, so there
is no second declaration that could disagree with the manifest.

`.github/workflows/release.yml` runs on every push to `master`. For each plugin it reads
the version from that plugin's manifest and checks whether the tag `{name}--v{version}`
already exists. If it does, nothing happens. If it does not, the workflow tags that
commit and cuts a GitHub release whose notes are the commits since that plugin's previous
tag, filtered to the paths that plugin actually ships — `memento` counts its own directory
plus the three skills; `auto-bottle` counts its own directory, `hooks/`, and
`skills/message-in-a-bottle/`. So a bump to one plugin does not attribute the other's work
to itself, tags are never moved, and the workflow is safe to re-run.

The tag shape matches what `claude plugin tag` creates and reads, so that CLI stays usable
against tags this workflow made.

To cut a release: bump the version in the plugin's `plugin.json`, merge to `master`, and
the workflow does the rest.

## License

MIT. Copyright (c) 2026 Brandon Fryslie. See [LICENSE](LICENSE).
