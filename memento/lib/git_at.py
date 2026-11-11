"""Runs git about a directory, so the repository that answers is the one the directory is in.

git takes its repository from GIT_DIR, GIT_WORK_TREE and the other variables it lists as local to a
repository before it looks at its working directory, and a process started from a git hook, or from
any shell that exported one, inherits them. [LAW:single-enforcer] Every git command about a
directory runs here.
"""

import functools
import os
import subprocess

# The settings `git -c` and GIT_CONFIG_COUNT carry, a safe.directory among them, are the user's for
# every repository: git keeps these two as it enters a submodule, so they reach the directory's too.
CONFIG_SETTINGS = frozenset({"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"})


@functools.cache
def _naming_a_repository():
    """The variables that name the repository git works in: those git lists as local to a repository,
    less the config settings it carries into another. Raises OSError where git cannot list them: where
    it exits non-zero, or answers without GIT_DIR, as a git older than the question does by echoing it
    back. Without the list, the inherited repository would answer."""
    listed = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True)
    named = frozenset(listed.stdout.split())
    if listed.returncode != 0 or "GIT_DIR" not in named:
        raise OSError(f"git cannot list the variables that name a repository, exiting "
                      f"{listed.returncode}: {listed.stderr.strip() or listed.stdout.strip()}")
    return named - CONFIG_SETTINGS


def git_at(directory, *arguments):
    """Runs `git <arguments>` in `directory`, returning the completed process with its output captured
    as text. Raises OSError where git cannot be run, or cannot list the variables that name a
    repository."""
    naming = _naming_a_repository()
    return subprocess.run(["git", *arguments], cwd=directory, capture_output=True, text=True,
                          env={name: value for name, value in os.environ.items() if name not in naming})
