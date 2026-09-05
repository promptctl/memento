Each version's section is written in the PR that bumps `.claude-plugin/plugin.json` beside this file; `claude plugin tag memento --push` then publishes it as the release notes. Procedure: https://github.com/promptctl/.github/blob/master/RELEASING.md

## v0.3.0 - 2026-09-05

- feat(address-pr-reviews): `wait()` reports whether the head was actually reviewed (`reviewed`, `not_reviewed_reason`), read from the reviewer's newest artifact; `review wait` exits nonzero on an unreviewed head, so a spent round cap halts the loop instead of merging green and unread
- test(address-pr-reviews): the bot-review jq filter runs through a real jq; the adversarial provider's `wait` and `review wait`'s halt gate are covered end to end

## v0.2.0 - 2026-09-03

- feat(next)!: retire the executable skill; /next is now a pointer stub, and the procedure ships with the lit binary (0.12.0+), which writes it to `.claude/skills/next/SKILL.md` on `lit init`

## v0.1.2 - 2026-09-02

- fix(message-in-a-bottle): clear the input box before writing into it
- test(message-in-a-bottle): keep the suite out of the real handoff directory

## v0.1.1 - 2026-09-02

- fix(message-in-a-bottle): never let a handoff die with the delivery

## v0.1.0 - 2026-09-01

- feat: extract memento into its own repo and marketplace

