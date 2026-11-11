---
name: journal
description: Deposit a note in the journal that outlives this session, or read back the short close-out summaries of the sessions that came before it. Use when the user says "journal this", "note that in the journal", "write that down for later", "what did the last session do", "why am I doing this", "what was already tried" — and unprompted when you have just made a decision whose reason will be lost at the reset, when you have abandoned an approach, or when you woke from a handoff that does not explain itself.
---

# Keep a journal across resets

A handoff carries the next step forward and nothing of how the work got there. The journal
keeps the rest. Writing to it is free; reading from it is small on purpose.

```bash
${CLAUDE_PLUGIN_ROOT}/skills/journal/bin/journal note '<text>'
${CLAUDE_PLUGIN_ROOT}/skills/journal/bin/journal note --file <path>
${CLAUDE_PLUGIN_ROOT}/skills/journal/bin/journal recall
```

## `note` deposits what the reset would lose

A short one-line note goes as one quoted argument; any other goes in a file, as the end of
this section explains. The journal stamps each entry itself with the UTC time, the session
id, the working directory and the branch checked out there, so leave those out. It prints
nothing and exits 0. Empty text or an empty file is refused, as are text and `--file`
together: exit 2, nothing written. A file that cannot be read is exit 1 with the reason.

Worth a note is whatever ends with this session and cannot be recovered from the code, the
diff, the ticket or the handoff: a decision and the reason for it, an approach tried and
abandoned and why, a measurement or a result, a trap invisible from the code. Narration of
what the diff already says is not, and neither is a progress update.

    WRONG: "Refactored the parser, tests pass."
    RIGHT: "Tried streaming the parse; abandoned it — the tokenizer needs the whole buffer
            to resolve heredocs. Went with a two-pass read instead."

No agent reads a note back. Notes are for the user, so one costs nobody tokens later: when
in doubt, deposit.

Claude Code's worktree guard reads the text of a Bash command, not files, and refuses a
command it cannot verify when that text names git or a shell (bash, sh, zsh and the like).
A one-line single-quoted note generally passes even so. A note of several lines, one that
needs an apostrophe or other quoting, or one the guard refused goes in a file: create it
with the Write tool outside the repository (under `/tmp`, say), since a heredoc or `echo`
puts the same text back into a command, and pass `--file <path>`. The journal stores the
file's contents as written, minus surrounding whitespace, and does not delete the file.
Don't respell a word (`g-i-t`, "the version-control tool") to get past the guard: the user
finds notes by searching for their words, and a respelled word is one they will not find.

## `recall` is the whole of what you read

Run it when you need to know why you are doing what you are doing, or what was already
tried, and the handoff you woke with does not say. It is not a start-of-session ritual.

It prints the close-out summaries of the sessions before this one in its chain of handoffs
— the session whose handoff woke you, the session whose handoff woke that one, and so on —
the nearest 5, oldest first. Each prints as a header line with the summary indented two
spaces beneath it:

    <UTC time>  <directory>  (<branch>)
      <summary>

A summary is at most 1,200 characters, so the whole read is bounded at about 6,000. It never
prints a note.

The last line is either
`Earlier close-outs in this chain, not shown: N.` or
`Nothing earlier: <why the chain goes no further>` — for example, that this session did not
wake from a handoff. That is an ordinary answer with exit 0, not an error. Exit 1 comes with
its reason: the journal could not be read, or `CLAUDE_CODE_SESSION_ID` is not set (a Claude
Code session's Bash tool sets it).

That output is all you read. Don't open, search or otherwise read the journal's file, and
don't go looking for another way to see notes. The journal exists so that depositing is
cheap and looking back is cheap, and reading the raw record spends the very tokens it was
built to save. The pull comes when recall's answer is thin and the fuller story feels one
file away. Stop there and tell the user that recall did not answer it; they can read the
full record.
