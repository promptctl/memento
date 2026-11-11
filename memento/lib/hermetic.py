"""The environment every test suite runs in: PATH of the one it was started from and a scratch HOME,
nothing else.

A suite started from a git hook or a Claude Code session inherits variables naming real state - a
hook's GIT_DIR its repository, a session's CLAUDE_CODE_SESSION_ID and MEMENTO_* its journal, log and
config - that code under test, or a command it runs, would act on. A suite calls `home` before it
imports the code it tests, as some of that code resolves its paths from the environment at import.
"""

import atexit
import os
import shutil
import tempfile


def home(prefix):
    """Replaces this process's environment with PATH and a new scratch directory as HOME, removed at
    exit, and returns the directory's path."""
    scratch = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)
    path = os.environ["PATH"]
    os.environ.clear()
    os.environ.update(PATH=path, HOME=scratch)
    return scratch
