Each version's section is written in the PR that bumps `.claude-plugin/plugin.json` beside this file; `claude plugin tag auto-bottle --push` then publishes it as the release notes. Procedure: https://github.com/promptctl/.github/blob/master/RELEASING.md

## v0.1.2 - 2026-09-02

- fix(message-in-a-bottle): clear the input box before writing into it
- test(message-in-a-bottle): keep the suite out of the real handoff directory

## v0.1.1 - 2026-09-02

- fix(message-in-a-bottle): never let a handoff die with the delivery

## v0.1.0 - 2026-09-01

- fix(auto-bottle): carry the current ceiling, and accept its own skill namespace
- feat: extract memento into its own repo and marketplace

