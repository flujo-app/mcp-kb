---
description: 'Python conventions for this repo'
applyTo: 'kubed/**/*.py,scripts/**/*.py,tests/**/*.py'
---
# Python conventions

Applies to every `.py` file here. Cross-cutting review priorities are in
[copilot-instructions.md](../copilot-instructions.md); this file is the
language-specific rules. These are hard conventions — flag violations.

## The supported range is 3.10 → 3.14

`pyproject.toml` declares `requires-python = ">=3.10"`, and `test.yml` sweeps the
whole range on main. So a 3.11+ syntax or stdlib feature is a **build break on
the oldest supported interpreter**, not a style question.

The ones that actually come up:

- `match` statements are 3.10, fine. `except*` groups are 3.11 — not fine.
- `typing.Self` and `asyncio.TaskGroup` are 3.11 — not fine.
- `tomllib` is 3.11 — not fine.
- `X | Y` in an annotation is fine on 3.10 **because every module starts with
  `from __future__ import annotations`**. Used at runtime — `isinstance(x, A | B)`,
  a `TypeAlias` value, a pydantic field evaluated eagerly — it is 3.10+ only in
  annotations and needs care elsewhere.

If a newer feature is genuinely worth it, the change is to raise
`requires-python` and drop matrix legs, in its own PR, deliberately. Not silently.

## Type hints in `tools.py` are a public contract

FastMCP derives each tool's JSON schema from the function signature, so an
annotation there is shipped to every model that lists the tools. Review it as an
API, not as a hint:

- Annotate every parameter and the return. A bare `dict` or a missing annotation
  produces a tool a model cannot call correctly.
- `param: str | None = None`, never `param: str = None`.
- Prefer a `Literal[...]` over `str` when the set of values is closed — it turns
  a guess into an enum in the schema.
- Docstrings here are **prompt**. Write them for a model choosing a tool.

## Coerce at the boundary

Arguments arrive from hand-written JSON, from form encoders, and from LLM tool
calls, so they arrive as strings.

- Use `as_bool` for anything boolean: `bool("false")` is `True`.
- Use `as_int` for anything numeric: `int("")` raises, and an omitted optional
  frequently arrives as `""`.
- A new parameter that reads a raw value straight out of the request is a finding.

## Style

- `from __future__ import annotations` at the top of every new module.
- `pathlib` over `os.path`; f-strings over `%` or `.format`.
- Module docstrings explain *why the module exists*, matching the existing files —
  not a restatement of the class names inside it.
- ruff is the linter and the gate (`ruff check kubed scripts`). `tests/` is
  excluded by `[tool.ruff]`; don't ask for lint compliance there.

## Errors

- **A wait that times out re-raises with a message naming what it was waiting
  for.** A bare `TimeoutException` reaching a caller is unactionable.
- Never swallow an exception to return a falsy value. The caller is usually a
  model, and a silent empty result is indistinguishable from a real one.
- Never put a token, a Grid URL with credentials, or a full request body into an
  exception message or a log line.

## Tests

- `tests/` runs with no browser and no network, against an unroutable Grid
  address. A test that reaches a real Grid is an integration test and is marked
  `@pytest.mark.integration`.
- A new action needs coverage on **both** surfaces. `tests/test_surfaces.py`
  asserts the tool set and the endpoint set are equal — if a change makes that
  fail, the fix is the missing half, never an edit to the assertion.
- `asyncio_mode = "auto"`, so an `async def test_` needs no decorator.
