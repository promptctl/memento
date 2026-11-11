"""Runs a lib module as a script: `<module>.py <command> <argument>...`, for the shell scripts that call
lib/transcript.py, lib/remote_control.py and lib/background_session.py.

A command is a function. [LAW:one-source-of-truth] Its signature is both its arity and its usage line, and
a `*rest` parameter takes any number of trailing arguments. What it returns is the exit status. A failure
the module names, or an OSError, prints its message and exits 2. Python exits 1 on an uncaught exception,
which a caller may read as a command's own 1, so any other exception prints its traceback and exits 2.
"""

import inspect
import os
import sys
import traceback


def _usage(name, command):
    return f"{name} " + " ".join(
        f"[<{p.name}>...]" if p.kind is p.VAR_POSITIONAL else f"<{p.name.replace('_', ' ')}>"
        for p in inspect.signature(command).parameters.values())


def _takes(command, count):
    parameters = list(inspect.signature(command).parameters.values())
    fixed = sum(p.kind is not p.VAR_POSITIONAL for p in parameters)
    return count == fixed or (count > fixed and any(p.kind is p.VAR_POSITIONAL for p in parameters))


def run(commands, failure, argv=None):
    """Runs the command argv names with the arguments after it, and exits with its status. `failure` is
    the exception type the module raises for a failure it names."""
    argv = sys.argv if argv is None else argv
    script = os.path.basename(argv[0])
    command, arguments = commands.get(argv[1] if len(argv) > 1 else ""), argv[2:]
    if command is None or not _takes(command, len(arguments)):
        print("usage: " + " | ".join(f"{script} {_usage(name, fn)}" for name, fn in commands.items()),
              file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(command(*arguments))
    except (failure, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(2)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
