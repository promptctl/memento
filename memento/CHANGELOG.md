Each version's section is written in the PR that bumps `.claude-plugin/plugin.json` beside this file; `claude plugin tag memento --push` then publishes it as the release notes. Procedure: https://github.com/promptctl/.github/blob/master/RELEASING.md

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

