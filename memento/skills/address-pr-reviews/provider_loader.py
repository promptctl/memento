#!/usr/bin/env python3
"""Loads the active PR review provider module and validates it against the provider contract.

The provider name comes from an explicit argument, then PR_REVIEW_PROVIDER, then provider.json in
this directory. The module is `<name>_provider.py` here and must declare CAPABILITIES and the
functions `wait` and `fetch`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent
CONFIG_FILE = SKILL_DIR / "provider.json"

# Maps each capability to the functions it requires. The required-key set and the validator both
# derive from this mapping, so a new capability is one entry here.
CAPABILITY_FUNCS = {
    "resolve":        ["resolve"],
    "trigger":        ["trigger"],
    "setup_check":    ["setup_check"],
    "dismiss_review": ["change_requests", "dismiss_review"],
}
REQUIRED_CAPABILITIES = set(CAPABILITY_FUNCS)


def get(name: str | None = None) -> object:
    """Returns the active provider module, validated against the contract.

    Precedence: the `name` argument, then PR_REVIEW_PROVIDER, then provider.json. An explicit
    argument selects a provider for that call only and does not change the default.
    """
    name = _resolve_name(name)
    module = _load_module(name)
    _validate(module, name)
    return module


def _resolve_name(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if env := os.environ.get("PR_REVIEW_PROVIDER"):
        return env
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())["provider"]
        except (json.JSONDecodeError, KeyError) as e:
            raise RuntimeError(f"provider.json is malformed: {e}") from e
    raise RuntimeError(
        f"No provider configured. Create {CONFIG_FILE} with "
        '\'{"provider": "<name>"}\' or set PR_REVIEW_PROVIDER.'
    )


def _load_module(name: str) -> object:
    path = SKILL_DIR / f"{name}_provider.py"
    if not path.exists():
        raise RuntimeError(
            f"Provider module not found: {path}\n"
            f"Available providers: {_available()}"
        )
    # Sibling modules such as github_threads become importable for every provider from here.
    if str(SKILL_DIR) not in sys.path:
        sys.path.insert(0, str(SKILL_DIR))
    spec = importlib.util.spec_from_file_location(f"{name}_provider", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise RuntimeError(f"Failed to load provider '{name}': {e}") from e
    return module


def _validate(module: object, name: str) -> None:
    caps = getattr(module, "CAPABILITIES", None)
    if caps is None:
        raise RuntimeError(f"Provider '{name}' is missing CAPABILITIES dict.")
    missing_keys = REQUIRED_CAPABILITIES - caps.keys()
    if missing_keys:
        raise RuntimeError(
            f"Provider '{name}' CAPABILITIES is missing keys: {missing_keys}"
        )
    for fn in ("wait", "fetch"):
        if not hasattr(module, fn):
            raise RuntimeError(
                f"Provider '{name}' is missing required function '{fn}'."
            )
    for cap, funcs in CAPABILITY_FUNCS.items():
        if not caps.get(cap):
            continue
        for fn in funcs:
            if not hasattr(module, fn):
                raise RuntimeError(
                    f"Provider '{name}' declares capability '{cap}' but does not "
                    f"implement the function '{fn}'."
                )


def _available() -> list[str]:
    return [
        p.stem.removesuffix("_provider")
        for p in SKILL_DIR.glob("*_provider.py")
    ]


if __name__ == "__main__":
    try:
        p = get()
        caps = p.CAPABILITIES
        print(f"Active provider: {p.__name__}")
        print(f"Capabilities:    {caps}")
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
