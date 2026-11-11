"""The environment every test suite runs in: PATH of the one it was started from and a scratch
directory as HOME and as the temp directory, nothing else.

A suite started from a git hook or a Claude Code session inherits variables naming real state - a
hook's GIT_DIR its repository, a session's CLAUDE_CODE_SESSION_ID and MEMENTO_* its journal, log and
config - that code under test, or a command it runs, would act on. A suite calls `home` before it
imports the code it tests, as some of that code resolves its paths from the environment at import.
"""

import atexit
import os
import shutil
import sys
import tempfile


def home():
    """Replaces this process's environment with PATH and a new scratch directory, named for the
    running suite and removed at exit, as HOME and TMPDIR, makes it the temp directory of every
    tempfile call that names none, and returns its path."""
    scratch = tempfile.mkdtemp(prefix=os.path.basename(sys.argv[0]) + ".")
    atexit.register(_remove, scratch)
    path = os.environ["PATH"]
    os.environ.clear()
    os.environ.update(PATH=path, HOME=scratch, TMPDIR=scratch)
    tempfile.tempdir = scratch
    return scratch


def _remove(scratch):
    """Removes the scratch directory, or fails the suite naming it: an exception in an exit handler
    leaves the exit status as it was. Registered as the suite starts, it runs after every exit handler
    the suite registers."""
    try:
        shutil.rmtree(scratch)
    except OSError as error:
        print(f"hermetic: cannot remove the scratch directory {scratch}: {error}", file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
