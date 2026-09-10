Each version's section is written in the PR that bumps `.claude-plugin/plugin.json` beside this file; `claude plugin tag memento --push` then publishes it as the release notes. Procedure: https://github.com/promptctl/.github/blob/master/RELEASING.md

## v0.7.0 - 2026-09-10

- feat!: the ceiling hook runs on `Stop` alone and denies nothing. On `PreToolUse` it read the count from the newest assistant record — true at a stop, stale on the first call after a tmux `/clear`, which resets a session in place, transcript and all. A session that closed out at 479,224 tokens read 479,224 again, had its first `Skill` call denied, and died having done no work. Gone with the gate: default-deny classification, shell-grammar parser, permitted-git allowlist, `ExitWorktree` escape hatch, misquoted-close-out rewrite message. 401 lines to 264. The cost is real: `Stop` only bites in a session that stops, so a long tool loop is no longer cut off mid-loop.
- feat!: `finalize-session` measures nothing. It always records the handoff, and resets the session only when passed `--reset clear|compact`. It used to decide for itself, scraping the hook's log for this session's last count and resetting whenever that number was at or past the ceiling — a second copy of the same stale number, which would have sent a just-reset session straight back round the loop. The stop instruction now names the flag it wants: `finalize-session --reset compact '<handoff message>'`. The flag resets on every transport; only clear-versus-compact is tmux-only, because iTerm2 and the detached transport relaunch a fresh process either way.
- feat!: nothing in the handoff message selects the reset mode. Given no `--reset`, the launcher used to read the message itself and infer `clear` or `compact` from a mention of `/compact` in the first three sentences, screened by a list of negations. That matcher had a documented misfire: "Do NOT use /compact — a blank slate is required" compacted, because a word-boundary match cannot see a negation, so the more emphatically an author forbade compaction the surer it was to happen. The screen leaves with the thing it was compensating for.
- fix(ceiling): a session already past the ceiling can raise its own ceiling, because the command that writes the session layer is no longer one the hook denies. The skill used to tell the agent to hand that command to the user to run in their own shell, which left the one session that needed headroom as the one that could not give itself any.

## v0.6.0 - 2026-09-10

- feat(next)!: the `next` skill is gone. It was only a pointer to the `/next` skill that `lit init` writes into a repository (lit newer than 0.11.0), and that project skill is the one to invoke. A repository lit has not initialized now has no `/next` at all, so there the `/next` handoff that `finalize-session` sends when given no message names a skill that does not exist.
- feat(ceiling): a `ceiling` skill moves the context ceiling for the session running right now (off, up by a signed amount, or pinned to a number) by writing that session's config layer, then reads the hook's log to confirm the write took. The confirmation is the point: a key the hook does not accept stops the gate without a word, and the log is the only place that shows it. Run it before the session breaches the ceiling, because past it the gate denies every Skill call except the close-out.
- fix(message-in-a-bottle): the close-out has one trigger the agent judges for itself - the handed unit of work is complete. It also told the agent to fire when it "came within reach of the context ceiling", and not to "wait to be forced", which asked it to estimate a number only the hook can see: the hook resolves the ceiling from the config layers and reads the live count from the transcript, and the agent has neither. Sessions were throwing their context away mid-work at a third of the ceiling on the strength of that guess. The token trigger belongs to the hook, which states both numbers in the instruction it hands over once a session is genuinely past the line.
- feat!: the `MEMENTO_CONTEXT_CEILING` environment variable is gone. Three layers set the ceiling, and every one of them is a file: user, project, session. A ceiling in force is now always something written down at a path you can open, rather than something a process was started with and nothing on disk records. A session that wants its own number writes the session layer, which is what that layer is for.
- feat!: `off` is the one word that turns the ceiling off. `none`, `never` and `disabled` were three more spellings of it, so a reader of a config file had four things to recognise where the writer only ever needed one.

## v0.5.1 - 2026-09-06

- fix(address-pr-reviews): the adversarial provider reads every page of a PR's reviews before answering whether its marker review is there. It refused any PR past 100 reviews, which is exactly the PR that has been through enough rounds to need it. `github_threads.paginated` is now the one reader of a paginated `gh api --jq` stream, shared with `bot_reviews`, so the per-page JSONL fact both depend on is stated once.

## v0.5.0 - 2026-09-06

- feat!: the one setting a `memento.conf` carries is spelled `ceiling`, and `context_ceiling` is not an alias for it. A config file that still sets the old key fails the way any unknown key does, naming the file and the line it read, so the rename surfaces as an error rather than as a ceiling that quietly reverted to the default. The `MEMENTO_CONTEXT_CEILING` environment variable keeps its name, and the default is still 250,000 tokens.
- docs: the README gives the recipe for a session that wants more headroom than the project allows — the session-layer path, the `CLAUDE_CODE_SESSION_ID` one-liner that writes it, and the signed value that adds to the project's ceiling instead of replacing it. Every piece of it was already in that section, spread across three paragraphs the reader had to combine.

## v0.4.0 - 2026-09-06

- feat!: `auto-bottle` is gone and memento is the only plugin. It shipped one skill file under a second namespace and symlinked the hook directory out of the repo root, so one skill answered to two names and every release meant two tags. Install `memento` and the ceiling hook comes with it; anyone who installed `auto-bottle` should uninstall it.
- feat: settings live in `memento.conf`, per user, per project and per session, and a signed `context_ceiling` adjusts the layer beneath it (was auto-bottle v0.2.0)
- fix: a written ceiling is settled by one declared shape. `o_f_f` switched the gate off, `context_ceiling = ²` raised a traceback instead of the crafted message, and `_350000` parsed as a number - all because the accepted shape was inferred from a strip, a slice and a predicate that each admitted a little more than the next (was auto-bottle v0.2.0)
- fix: a session id becomes a path in one place, checked by where it resolves rather than how it is spelled. `..`, `./..` and `..//` read the user's own config as the session layer, applying one file as two layers (was auto-bottle v0.2.0)
- fix: the close-out skill has one name, so the hook no longer carries a set of two to cover a second plugin's namespace

## v0.3.0 - 2026-09-05

- feat(address-pr-reviews): `wait()` reports whether the head was actually reviewed (`reviewed`, `not_reviewed_reason`), read from the reviewer's newest artifact; `review wait` exits nonzero on an unreviewed head, so a spent round cap halts the loop instead of merging green and unread
- test(address-pr-reviews): the action provider's reviewed-verdict (`parse_agent_artifact`, `head_review_verdict`, `wait()` through a fake `gh`), the bot-review jq filter through a real jq, the adversarial provider's `wait`, and `review wait`'s halt gate are covered end to end

## v0.2.0 - 2026-09-03

- feat(next)!: retire the executable skill; /next is now a pointer stub, and the procedure ships with the lit binary (0.12.0+), which writes it to `.claude/skills/next/SKILL.md` on `lit init`

## v0.1.2 - 2026-09-02

- fix(message-in-a-bottle): clear the input box before writing into it
- test(message-in-a-bottle): keep the suite out of the real handoff directory

## v0.1.1 - 2026-09-02

- fix(message-in-a-bottle): never let a handoff die with the delivery

## v0.1.0 - 2026-09-01

- feat: extract memento into its own repo and marketplace

