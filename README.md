# memento

A Claude Code marketplace with one plugin in it. It is about one problem: an agent
session has a beginning, a middle, and an end, and the ends are where work gets lost —
a PR review half-addressed, a session that hits the context limit and forgets what it
was doing.

`memento` gives you three skills you invoke by hand: work a PR review to clean, write a
handoff for the next session, move the context ceiling for this session or its project.
It also ships one hook, which takes the handoff skill and makes it mandatory — past a
token ceiling, a session cannot end its turn until it has closed out: written the handoff
and reset.

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
`memento/skills/message-in-a-bottle/bin/finalize-session`, which records the handoff to
disk and, with `--reset`, also schedules a delayed handoff into your own session: the
session resets, and the message you wrote arrives as the next agent's opening prompt.
Without the flag it prints the path it wrote and leaves the session running, so writing
the message need not cost you your context.

```bash
finalize-session [--goal '<condition>'] [--reset clear|compact] [--] [message...]
```

With no message it hands off `/next`. `clear` starts the next session blank and `compact`
starts it with a compacted summary — a distinction only the tmux transport can honour,
since the other two launch a fresh process and are blank by construction.
`--goal` re-issues an active `/goal` condition into the reset session, which otherwise
dies silently at the handoff and stops an unattended run.

`--help` prints a usage block and exits 0 recording nothing, and any other two-dash
word it does not know is refused — named on stderr, exit 2, no handoff written —
instead of being swallowed as message text. Only a two-dash word is read as a flag, so
`-h`, a recap opening `- shipped the parser`, and anything carrying a newline all
record as the message they are. Put `--` first when the message begins with a two-dash
word, whatever comes after it: `finalize-session -- --already-fixed see PR 123`.

When it does reset, the launcher picks its transport by capability: reset the tmux pane
in place, else kill and relaunch the iTerm2 session, else spawn a fresh detached tmux
window. Prefix `FINALIZE_DRY_RUN=1` to see which one it would choose without scheduling
anything.

**`ceiling`** — moves the context ceiling, for the session running right now or for its
whole project: off, up or down by a signed amount, or pinned to a number. It calls
`memento/skills/ceiling/bin/ceiling`, which writes the config layers described below.

```bash
ceiling show
ceiling set <session|project> <off|N|+N|-N>
ceiling clear <session|project>
```

The scope is a required word with no default, because the two differ in blast radius — a
project change leaves a file in the repo that outlives the session, a session change leaves
nothing that does — and nothing in a person's phrasing reliably says which they meant. Every
command ends by printing the ceiling in force for this session, the ceiling a new session
here would get, and the file behind each layer, so the command's own output is the
confirmation that the write took.

## The context ceiling

What makes the close-out fire without being asked is
`memento/hooks/scripts/context-ceiling.py`, registered on `Stop`.

At a stop the hook reads the transcript for the most recent assistant message's token
usage (all four fields — input, output, cache creation, cache read — because that is what
the next request carries) and compares it to the ceiling. Under the ceiling, the hook says
nothing.

`Stop` is the one event where that count is true. It describes the live context only at
the moment a turn ends: a session reset in place keeps its session id and its transcript
file, so mid-turn the newest record can still belong to a context that was already thrown
away. A payload for any other event stops the hook with an error naming the event rather
than measuring anyway. The cost of running on one event is that a session working through
a very long tool loop is not caught until that turn ends.

The ceiling is 250,000 tokens by default, and three layers can move it. From the least
specific to the most: `~/.config/promptctl/memento.conf`, then the nearest
`.promptctl/memento.conf` at or above the project directory, then
`~/.config/promptctl/sessions/<session-id>/memento.conf`. Same filename everywhere, so a
second setting is one more key rather than one more file, one more lookup and one more
precedence chain. Every layer is a file, so a ceiling in force is always written down
somewhere you can open. The home-rooted layers follow `XDG_CONFIG_HOME` where it is set,
and `MEMENTO_CONFIG_HOME` moves them outright.

Each file is `key = value` lines, `#` starts a comment, and `ceiling` is the only key
so far:

```
# this repo runs long
ceiling = 350_000
```

It takes a count, a signed adjustment, or `off` in place of a number, which turns the
ceiling off entirely. An adjustment applies to
whatever the layers beneath it resolved to, which is what lets one session delay its own
handoff without touching — or knowing — the number the project pinned:

```
ceiling = +100_000
```

The two shared layers — the user file and the project file — are read once per session,
at its first stop, and what they resolve to together is written down as
`~/.config/promptctl/sessions/<session-id>/shared-at-start.conf`, an ordinary config file
in the same format holding an absolute number or `off`: `ceiling = 350000`. From then on
that record is the shared contribution for that session, and neither shared file is read
again for it. Nothing removes that record. A session that stops even once leaves one
small file under `~/.config/promptctl/sessions/`, and it stays there after the session is
gone. Editing a shared file, deleting it, or leaving a syntax error in it does not move a
running session's ceiling and cannot gate it. A session that starts afterwards reads the
shared files as they stand: an edit or a deletion gives it the new number, and a syntax
error stops the hook for that session with an error, the same loud failure a malformed
config has always produced.

That split is here because of one afternoon. A shared file held `350000` from 05:18 on
2026-09-06 until an agent working in an unrelated project removed the line at 14:55, and
every running session read the default 250,000 from its next tool call onward. One of
them was at 250,196 tokens, mid-epic, in a different directory. It went from unrestricted
to fully gated between two consecutive tool calls, could not reach the remedy from inside
the gate, and never wrote a handoff. The record is made at the first stop rather than at
the first token because `Stop` is the only event this hook is given — one turn of drift,
spent where a session is still far below any ceiling.

The record is keyed on the session id and is never rewritten, so a session that closes
out and carries on keeps the ceiling it started under.
`finalize-session --reset clear|compact` sends `/clear` or `/compact` as keystrokes into
the same running process: the process survives and the session id with it, so the context
after the reset is a new context under an old record. A later change to a shared file
never reaches it, however much it looks from the pane like a session that started
afterwards. Its own layer still applies immediately, so a session in that position can
still give itself room.

The session's own layer is read live at every stop and applies immediately in both
directions, raising and lowering alike. `ceiling set session +100_000` writes that layer
for the session running right now. Because the value is signed it lands on top of what the
shared layers resolved to rather than replacing it — a session that started at 250,000
resolves to 350,000 — and it takes effect the next time the ceiling is checked, when the
turn ends, with nothing to restart, reload or signal, including from a session that is
already over the ceiling. `ceiling set session -50_000` lowers it the same way: the leading `-`
is read as part of the value rather than as an unknown option, on every Python the plugin runs
under. The two help flags are the one exception, so `ceiling set session -h` prints usage rather
than complaining about a ceiling spelled `-h`. `ceiling clear session` hands the session back to
the ceiling it started under, not to whatever the shared files say now: `shared-at-start.conf` is
still standing.

The command is worth going through rather than writing that file yourself, for one reason:
the write succeeds whatever you put in the file, and a key the hook does not accept does
not leave the old ceiling standing — it stops the hook, which Claude Code treats as
non-blocking, so the gate quietly stops running for the session that wrote the file.
Everything the command writes goes out through one function that renders only what the
hook's own parser accepts and then reads the file back, and both programs read that parser
out of `memento/lib/ceiling_config.py`, so the write that switches the gate off is not one
the command can make.

The project layer is found by walking up, so a subdirectory or a worktree inherits the
repo above it, and it is anchored at `CLAUDE_PROJECT_DIR` where Claude Code sets it — one
function the hook and the command both call, differing only in the directory each falls
back to — so the ceiling cannot change because something ran `cd`. Anything else — a typo,
a unit suffix, a misspelled key, a key set twice in one file, an adjustment resolving below
zero — stops the hook with an error naming the file and line the setting came from. It
never falls back quietly to the default, on the grounds that a ceiling you believe you moved and did not
is worse than no ceiling. Every decision the hook makes is appended to
`~/.claude/memento/context-ceiling.log` (`MEMENTO_CEILING_LOG`), which is the only place
you can tell an allow apart from a hook that never ran.

`ceiling set project` moves the project's ceiling instead, and writes two files to do it.
The shared layers are frozen per session, so the project file alone would move the ceiling
for every session after this one and not for the session that asked — which is the session
that wanted headroom. Both files get the same resolved absolute number rather than the same
adjustment, and that is what keeps them from drifting apart: an absolute ignores the layers
beneath it. Each is written beside its destination and moved into place only once both are
staged, so the failures that actually happen — no permission, no space, a parent that cannot be
made — land before either file is in force and the command exits having moved nothing rather
than leaving two files stating different ceilings. However that pass ends, the staged files it
still holds go with it, so a halt part way through leaves no `memento.conf.<pid>` beside a
project's config that nothing here reads and nobody would notice. Each `wrote` line names what
that file held before it — `wrote .promptctl/memento.conf line 1 (replacing 900000)` — because
this session's layer is one of the two, so a project-scoped move can overwrite a ceiling an
earlier `ceiling set session` pinned, and that parenthetical is the only record of the number
that was there. The file it rewrites is whichever project config that walk already finds in
force, so no second file appears deeper in the tree where the walk would reach it first and
two files would claim one ceiling. Where none stands it creates one at the repository root,
found by asking git — from the anchored directory, not from wherever the process happens to
stand — for the *common* dir, so a session working in a worktree writes the checkout that
worktree belongs to rather than a file that dies with the worktree. Inside a submodule it asks
git first whether there is a superproject and writes the submodule's own working-tree root
instead, because a submodule's common dir is the superproject's `.git/modules/<name>`, whose
parent is git's own internal storage — a directory no walk up from the submodule ever passes
through, so a file written there would be reported as written and govern nothing. Asked from
the process's own directory, as that one call used to be, a shell that had `cd`'d out of the
session's project could land the new file in the wrong repository, or refuse while the anchored
directory was a perfectly good repo. Outside a git repository there is no root to create that
file at, so `ceiling set project` refuses and names the anchored directory it asked about;
`ceiling clear project` needs no repository at all, and the session scope still works. The file
is not gitignored, and committing it is your call.

Two sessions moving the project's ceiling at once get a refusal rather than a lost write. The
project layer is shared — every session in the checkout reads it, and since this command exists
any of them can write it — so `ceiling set project +50_000` run from two sessions at once would
otherwise read the same number twice and the later write would silently drop the earlier one's
change. Each destination is read once before the move is resolved and again immediately before
it is written, and one that changed in between stops the command: nothing is written, the change
that landed stands, and the refusal names both numbers so you can run it again. Every
destination is checked before any is committed, so a refusal cannot leave one file written and
the other not, and a lock would not do this job: these files are meant to be edited by hand, and
a hand takes no lock.

`ceiling clear project` removes the project layer and this session's. Later sessions return
to the layer beneath the project; this one returns to the ceiling it started under. What
each file held is printed as it goes, so a number someone meant to keep is recoverable from
the output. It removes the project config in force where one stands and nothing where none
does, so in a project that never set a ceiling it prints this session's layer alone, and the
report's `project layer     (none)` line is what says the project layer is unset. A layer too
broken to parse is still one it can take away: it prints what it could not read and removes the
file. It used to die validating the file it was asked to delete, which left a malformed layer —
the gate already off for that session, a stopped Stop hook being non-blocking — removable by
nothing but hand. `ceiling set` replaces one too, where the request does not need what is
beneath: a count or `off` states a ceiling outright and never reads the layer it overwrites,
while a signed adjustment has no base to add to and refuses. Anywhere a ceiling is resolved, a
malformed layer is still refused loudly.

Over the ceiling, the hook returns `{"decision": "block"}`. Claude Code refuses the stop
and hands the hook's `reason` back to the agent as its next instruction: commit or push
everything outstanding first, then run the `finalize-session` launcher with a handoff
message and `--reset compact`. The launcher always writes the handoff to disk; the flag is
what makes recording it also reset the session.

It forces this **once per session**. If the session stops again, the hook sees
`stop_hook_active` and lets the stop proceed, printing a visible system message saying
the one forced attempt was spent. A second block would spend more context on the problem
that *is* too much context.

## Repo layout

One plugin, one directory, and every file is a real file:

```
.claude-plugin/marketplace.json
memento/.claude-plugin/plugin.json
memento/CHANGELOG.md
memento/hooks/hooks.json
memento/hooks/scripts/context-ceiling.py
memento/lib/ceiling_config.py
memento/skills/address-pr-reviews/
memento/skills/message-in-a-bottle/
memento/skills/ceiling/
```

`memento/lib/` is the one thing two parts of the plugin share: the hook and
`memento/skills/ceiling/bin/ceiling` both read the config layers through
`ceiling_config.py`, so the program that gates a session on its ceiling and the program
that moves one cannot disagree about the file format or about which layer wins.

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
