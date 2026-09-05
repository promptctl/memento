# PR Review Provider Contract

A provider is a Python module that translates one review backend into the
canonical operations the `address-pr-reviews` skill consumes. Adding a new
provider means writing one module; no changes to the skill are required.

## Module layout

Every provider module lives in the same directory as this file and is named
`<name>_provider.py`. It exposes:

- A top-level `CAPABILITIES` dict (see below).
- The functions its capabilities declare as supported.
- `wait` and `fetch`, which are always required.

The loader (`provider_loader.py`) imports the module, reads `CAPABILITIES`,
and returns the module. The skill calls the functions directly — no class
hierarchy, no base class, no dynamic dispatch beyond the import.

## Provider selection

`provider.json` in this directory names the active provider:

```json
{ "provider": "adversarial" }
```

The env var `PR_REVIEW_PROVIDER` overrides the file. The loader maps the name
to `{name}_provider.py`. Switching providers = edit one value.

## CAPABILITIES

```python
CAPABILITIES = {
    "resolve":        True,   # provider.resolve() is implemented and callable
    "trigger":        False,  # provider.trigger() is implemented and callable
    "setup_check":    True,   # provider.setup_check() is implemented and callable
    "dismiss_review": True,   # change_requests()/dismiss_review() are implemented
}
```

All four keys are required. `True` means the capability's function(s) are present
and must be called where the skill would call them. `False` means they are absent;
the skill skips the corresponding step and notes the gap to the agent.

The capability→function mapping is declared once in `provider_loader.py`
(`CAPABILITY_FUNCS`) and the loader validates every declared capability has its
functions. `dismiss_review` is the one capability backed by two functions
(`change_requests` + `dismiss_review`) — the fetch-time read and the verified
mutation that pair the way `fetch` + `resolve` do.

## Required functions

### `wait(pr_url: str) -> dict`

Blocks until the review for the PR's current head SHA is complete.

```python
# return value
{
    "status":     "completed",          # always "completed" on success
    "conclusion": "success" | "...",    # provider-specific string; skill surfaces non-success
    "sha":        "abc123",             # the head SHA the run was for
    "url":        "https://..." | None, # link to the review run, if available
    "reviewed":   True | False,         # did the reviewer actually review `sha`?
    "not_reviewed_reason": None | str,  # None exactly when reviewed is True
}
```

`not_reviewed_reason` is a `str`, not a closed union: whatever reason the
reviewer's own not-reviewed marker names passes through verbatim (today
`"round-cap"` and `"fork"`; a reason the reviewer adds later still halts the
loop, it just names itself), plus `"no-review-for-head"` from the provider
when the run completed and the reviewer's newest artifact reviews some other
commit or nothing is there.

`reviewed` is a separate fact from `conclusion`. A run can complete
successfully without reading the head — the GitHub Action reviewer exits 0 on
a spent `MAX_REVIEW_ROUNDS` cap by design (a cost control must not red a
required check) and says so on the PR with a marked not-reviewed review — and
such a run has zero findings for the same reason a clean review does. The
skill halts on `reviewed: False` exactly as it halts on a non-success
conclusion; `not_reviewed_reason` names why, in the reviewer's own vocabulary
where it has one. `[LAW:no-silent-failure]`

Raises `RuntimeError` with a human-readable message on timeout or unrecoverable
failure. Never returns a dict with `status != "completed"` — callers do not poll.

For providers that review synchronously inside `fetch`, implement `wait` as a
no-op that returns `{"status": "completed", "conclusion": "success", "sha": "",
"url": None, "reviewed": True, "not_reviewed_reason": None}` and document that
behavior — a synchronous provider's `wait` verifying its own review exists IS
the proof the head was reviewed.

### `fetch(pr_url: str) -> dict`

Returns every pending finding on the PR in canonical form.

```python
# return value
{
    "findings": [
        {
            "file":            "path/to/file.py",  # nullable (file-level comment)
            "line_start":      42,                  # nullable
            "line_end":        42,                  # nullable
            "body":            "...",               # required; first comment body
            "author":          "github-actions",    # required
            "thread_id":       "PRRT_xyz...",       # nullable if resolve=False
            "is_resolved":     False,               # required
            "thread_comments": [                    # required; may be one element
                {"author": "...", "body": "..."}
            ],
        }
    ]
}
```

`thread_id` MUST be non-null for every finding when `CAPABILITIES["resolve"]`
is `True`. It is null-safe to omit when `resolve` is `False`.

The skill treats `is_resolved: false` findings as unresolved. The loop exits
when this list is empty. Providers must not filter findings server-side — the
skill owns that judgment.

## Optional functions

### `resolve(thread_id: str) -> dict`

Required when `CAPABILITIES["resolve"]` is `True`.

Marks one finding as addressed and verifies the backend confirmed it.

```python
# return value
{"thread_id": "PRRT_xyz...", "is_resolved": True}
```

Raises `RuntimeError` if the backend did not confirm resolution. Must not
return successfully unless confirmation was received — a silent no-op here
is a bug that leaves threads open forever.

### `trigger(pr_url: str) -> dict`

Required when `CAPABILITIES["trigger"]` is `True`.

Explicitly requests a review from the backend.

```python
# return value
{"triggered": True}
```

When `trigger` is `False`, the provider fires on push automatically — the
skill does not call this function.

### `change_requests(pr_url: str) -> dict`

Required when `CAPABILITIES["dismiss_review"]` is `True`.

Returns the automated reviewer's blocking reviews — the `CHANGES_REQUESTED`
reviews the current round must dismiss once its findings are addressed.

```python
# return value
{
    "reviews": [
        {"review_id": 4484265295, "author": "github-actions[bot]",
         "commit_id": "abc123..."}
    ]
}
```

Scoped to the automated reviewer, never a human: a human's `CHANGES_REQUESTED`
is cleared only by that human re-reviewing. The skill reads this at fetch-time
and dismisses by id at round end, so a push's fresh re-review (a new id this read
never saw) is never wrongly dismissed. `[LAW:one-source-of-truth]`

### `dismiss_review(pr_url: str, review_id: int, message: str) -> dict`

Required when `CAPABILITIES["dismiss_review"]` is `True`.

Dismisses one stale `CHANGES_REQUESTED` review with an explanatory message and
verifies the backend recorded the dismissal.

```python
# return value
{"review_id": 4484265295, "is_dismissed": True}
```

Raises `RuntimeError` if the backend did not confirm the review reached the
`DISMISSED` state. Must not return successfully otherwise — a silent no-op here
leaves the PR blocked by a review the round was supposed to clear. `[LAW:no-silent-failure]`

### `setup_check(owner: str, repo: str) -> dict`

Required when `CAPABILITIES["setup_check"]` is `True`.

Verifies the provider's prerequisites for this repo.

```python
# return value on success:   {"installed": True,  "message": "..."}
# return value on failure:   {"installed": False, "message": "human-readable fix hint"}
```

The skill calls this once before the loop and halts with `message` if
`installed` is `False`. `[LAW:no-silent-failure]`

## Error contract

All functions raise `RuntimeError` (with a message the skill can surface
verbatim) or `subprocess.CalledProcessError` for shell-out failures. They
never return a partial result and rely on the caller to detect the error —
that is a silent failure. `[LAW:no-silent-failure]`

## Adding a new provider

1. Create `<name>_provider.py` in this directory.
2. Set `CAPABILITIES` accurately — don't declare a capability unless the
   function is implemented.
3. Implement `wait` and `fetch` (always required) plus any declared optional
   functions.
4. Set `provider.json` to `{"provider": "<name>"}` to activate it.
5. Test `wait` + `fetch` against a real PR before declaring it production-ready.
