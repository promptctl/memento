"""Runs git about a directory, so the repository that answers is the one the directory is in.

git takes its repository from GIT_DIR, GIT_WORK_TREE and the other variables it lists as local to a
repository before it looks at its working directory, and a process started from a git hook, or from
any shell that exported one, inherits them. [LAW:single-enforcer] Every git command about a
directory runs here.
"""

import functools
import os
import subprocess


@functools.cache
def _local_to_a_repository():
    """The variables git lists as local to a repository, the ones git clears itself before it works in
    another repository. Raises CalledProcessError where git cannot list them: without the list, the
    inherited repository would answer."""
    listed = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True,
                            check=True)
    return frozenset(listed.stdout.split())


def git_at(directory, *arguments):
    """Runs `git <arguments>` in `directory`, returning the completed process with its output captured
    as text. Raises OSError where git cannot be run."""
    local = _local_to_a_repository()
    return subprocess.run(["git", *arguments], cwd=directory, capture_output=True, text=True,
                          env={name: value for name, value in os.environ.items() if name not in local})
