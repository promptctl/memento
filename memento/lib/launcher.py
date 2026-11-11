"""What a command that runs the message-in-a-bottle launcher, finalize-session, is.

[LAW:one-source-of-truth] The close-out-call hook, which keeps a close-out in the foreground, and the
context-ceiling hook, which credits one, both read it here, so a call one credits the other sees.
"""

import re

# `finalize-session` as a word: no letter, digit, `.`, `-` or `_` on either side, so a call by path,
# by name or inside a wrapper's command matches and `finalize-session.test.py` does not. It is lexical,
# not a shell parse: a parse that misses a real call lets a backgrounded close-out through silently,
# while every shape a parse can miss (a backslash-newline, a newline separator, `)&&`, a heredoc)
# still names the word. A command that only mentions the word matches too.
LAUNCHER_WORD = re.compile(r"(?<![\w.-])finalize-session(?![\w.-])")


def names_launcher(command):
    """Whether `command` names the launcher as a word."""
    return LAUNCHER_WORD.search(command) is not None
