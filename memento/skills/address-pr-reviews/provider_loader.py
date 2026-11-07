#!/usr/bin/env python3
"""Builds the active PR review provider and validates it against the provider contract.

The provider name comes from an explicit argument, then PR_REVIEW_PROVIDER, then provider.json in
this directory. The module is `<name>_provider.py` here; its `Provider` class is built with the
caller's ports and must declare CAPABILITIES and the operations `wait` and `fetch`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent
# Sibling modules such as github_threads become importable for every provider and caller from here.
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from ports import Ports  # noqa: E402

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


def get(name: str, ports: Ports) -> object:
    """Returns the provider `name` names, built with `ports` and validated against the contract.

    `resolve_name` says which name is configured. The caller builds `ports`, so each entry point
    is the one place that decides which gh and which clock its provider runs on.
    """
    provider = _provider_class(name)(ports)
    _validate(provider, name)
    return provider


def resolve_name(explicit: str | None) -> str:
    """Returns the provider name: `explicit`, then PR_REVIEW_PROVIDER, then provider.json. An
    explicit name selects a provider for that call only and does not change the default."""
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


def _provider_class(name: str) -> type:
    path = SKILL_DIR / f"{name}_provider.py"
    if not path.exists():
        raise RuntimeError(
            f"Provider module not found: {path}\n"
            f"Available providers: {_available()}"
        )
    spec = importlib.util.spec_from_file_location(f"{name}_provider", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise RuntimeError(f"Failed to load provider '{name}': {e}") from e
    if not isinstance(getattr(module, "Provider", None), type):
        raise RuntimeError(f"Provider module '{name}' defines no Provider class.")
    return module.Provider


def _validate(provider: object, name: str) -> None:
    caps = getattr(provider, "CAPABILITIES", None)
    if caps is None:
        raise RuntimeError(f"Provider '{name}' is missing CAPABILITIES dict.")
    missing_keys = REQUIRED_CAPABILITIES - caps.keys()
    if missing_keys:
        raise RuntimeError(
            f"Provider '{name}' CAPABILITIES is missing keys: {missing_keys}"
        )
    for fn in ("wait", "fetch"):
        if not hasattr(provider, fn):
            raise RuntimeError(
                f"Provider '{name}' is missing required operation '{fn}'."
            )
    for cap, funcs in CAPABILITY_FUNCS.items():
        if not caps.get(cap):
            continue
        for fn in funcs:
            if not hasattr(provider, fn):
                raise RuntimeError(
                    f"Provider '{name}' declares capability '{cap}' but does not "
                    f"implement the operation '{fn}'."
                )


def _available() -> list[str]:
    return [
        p.stem.removesuffix("_provider")
        for p in SKILL_DIR.glob("*_provider.py")
    ]


if __name__ == "__main__":
    from ports import production_ports

    try:
        name = resolve_name(None)
        p = get(name, production_ports())
        print(f"Active provider: {name}")
        print(f"Capabilities:    {p.CAPABILITIES}")
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
