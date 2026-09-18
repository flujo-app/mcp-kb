# Contributing

## Getting set up

```bash
git clone --recurse-submodules git@github.com:kubed-io/mcp-kb.git
pip install -e ".[test]"
ruff check .
pytest
```

`ruff check .` covers the whole checkout, tests included — the rule set and the
one documented exception live in `[tool.ruff.lint]` in `pyproject.toml`, so there
is no second list of paths to keep in sync. The line limit is 88 columns.

**Python 3.11 is the floor**, and a pull request is tested on 3.11 and 3.14 —
both ends of the range, because every version break this project has had lives at
the old end and would otherwise land on main green. A merge to main and every
release sweep 3.11 through 3.14. So a 3.12+ construct is a build break on the
oldest leg rather than a style question: an f-string cannot carry a backslash in
its expression, and a glob ending in `**` is written `dir/**/*` because a
trailing `**` matches directories only before 3.13.

`kubed/` is a **PEP 420 namespace package** and has no `__init__.py`. That is
what lets `kubed.mcp_kb` and the sibling `kubed.selenium_flow` install side by
side; `tests/test_packaging.py` fails if one appears.

The submodule is the GitHub wiki, checked out at `wiki/`. An existing clone picks
it up with `git submodule update --init`. Nothing in the build needs it, so a
clone without it works — the wiki tests skip and you have no wiki.

The tests use a synthetic skills tree from `tests/conftest.py` and never clone an
upstream repository, so the suite needs no network. `tests/webdav_server.py`
stands up a real WebDAV server on loopback for the WebDAV backend; that is as far
out as anything reaches.

## Where things live

| Path | Holds |
|---|---|
| `kubed/mcp_kb/` | `server.py` composes it and owns which snapshot is current, `main.py` starts it, `routes.py` is the plain-HTTP surface, `config.py` is the config model and the published schema |
| `kubed/mcp_kb/plugins/` | what the config declares and a marketplace publishes, resolved: `address.py` is the address grammar, `marketplace.py` reads somebody else's catalogue, `manifest.py` a plugin's own `plugin.json`, `select.py` the category and tag query |
| `kubed/mcp_kb/catalogue/` | what is served and how it is found: `harvest.py` decides what counts, `skills.py` is the domain, `uris.py` is the `skill://` address space, `prompts/` parses a prompt file in any of three dialects with no FastMCP in it, `placeholders.py` resolves the two `${CLAUDE_*}` this server knows, `index.py` persists it, `snapshot.py` builds one immutable view, `refresh.py` decides when to look again |
| `kubed/mcp_kb/sources/` | where bytes come from, one fetch at a time: `file.py`, `git.py`, `webdav.py`, the shared `export.py`, and `live.py` because revalidation is a source concern |
| `kubed/mcp_kb/mcp/` | what an agent sees: `resources.py` is the interface, `tools.py` and `prompts.py` the mirrors, `request.py` and `scope.py` the per-request scope, `pins.py` the refusal of a scope that names nothing, `announce.py` the list-changed notification |
| `kubed/mcp_kb/spec/` | the OpenAPI document for the HTTP surface |
| `examples/config.yaml` | the worked config the image ships, and what `tests/test_example_config.py` validates |
| `wiki/` | the GitHub wiki, as a submodule — depth the README has no room for |
| `wiki/notes/` | hand-written prose folded into the generated wiki pages |

Two rules that the layout is there to protect:

- **Nothing outside `main.py` and `EnvRef.resolve` reads the environment.** A
  credential is resolved where it is declared, so it never passes through a
  plain `str` on the way to a client.
- **Every listing and every read takes a `Scope` and decides about it.** There is
  no method in the catalogue that can be called without one, which is what keeps
  a second code path from quietly forgetting.

## The wiki is generated

`scripts/generate_wiki.py` renders three pages in-process from the live objects:
`Configuration.md` from `config.schema()`, `Tools.md` from a real server's tool
list, and `Endpoints.md` from `spec.build_spec()`. They cannot describe a server
that never shipped.

```bash
python scripts/generate_wiki.py            # write the pages
python scripts/generate_wiki.py --check    # fail if they are out of date
```

`.github/workflows/wiki.yml` regenerates and pushes on every change to `kubed/`,
`wiki/` or the generator. A pull request generates but never pushes, and
`workflow_call` leaves the decision to the caller.

Staleness is caught by `tests/test_wiki.py`, not by that workflow: `test.yml`
checks out the submodule so those tests run, and a pull request that forgot to
regenerate fails the suite where a contributor is already looking.

The other pages — Home, Installing, Deployment, Sources, Plugins, Skills,
Prompts, Scoping, Operations — are hand-written in full and the generator never
touches them. Prose a schema cannot carry that belongs to a *generated* page goes in
`wiki/notes/<page>.notes.md` and is folded in. The suffix is load-bearing: a wiki
page is addressed by basename regardless of directory, so `wiki/notes/Tools.md`
and `wiki/Tools.md` would both answer to `/wiki/Tools` and GitHub would serve the
fragment. `tests/test_wiki.py` fails on any such shadowing.

## The spec is generated too

`openapi.yaml` is a **build artifact and is gitignored**. The server builds the
same document per process at `GET /openapi.yaml`, and the tests and the wiki
generator build it in-process, so nothing needs the file. Write a copy when you
want one to read, lint or publish:

```bash
python scripts/generate_openapi.py
```

Changes go in `kubed/mcp_kb/spec/`, never in the generated file. Response shapes
are hand-maintained there — the routes return plain dicts, so there is nothing to
introspect — and `tests/test_openapi.py` is what catches them drifting from what
`routes.py` actually returns.

`config.schema.json` is the same kind of thing for the config models, except that
it *is* committed: regenerate it with `python -m kubed.mcp_kb schema >
config.schema.json` (`mcp-kb schema` once the console script is installed) and a
test fails when it and the models disagree.

## Changing what a client sees

A client caches tool and resource schemas and cannot reliably be told to read
them again — `notifications/tools/list_changed` exists and caching clients ignore
it, and a sessionless connection never receives one at all.

So a tool argument never changes shape in one step. Either accept both forms for
a release and say which one is going, or give the new shape a new name. Adding an
optional argument is safe; renaming, retyping or removing one is not. The same
goes for the `skill://` grammar: a URI somebody's agent has written down must go
on resolving.

## What CI will say about it

A pull request runs these, and they are required to merge:

| Check | What it is |
|---|---|
| `PR Tasks` | assigns you, and fails if `CHANGELOG.md` has no new `[Unreleased]` entry — that section becomes the release notes. The `no changelog` label is the escape hatch |
| `Test (3.11)` / `Test (3.14)` | `ruff check .` and the full pytest suite, at both ends of the supported range |
| `Package` | builds the sdist + wheel, `twine check --strict`, then installs the wheel clean and imports it |
| `CodeQL` / `Dependency Audit` / `Workflow Audit` / `Dockerfile Lint` / `OpenAPI Spec` | `quality.yml` — code scanning, `pip-audit`, `zizmor`, `hadolint`, and a Redocly lint of the generated spec |
| Copilot review | reviews against `.github/copilot-instructions.md` |

The image is **not** built on a pull request: a two-architecture build is most of
a pull request's wait for a signal the merge build gives anyway.

When a quality gate is wrong rather than you, the fix is a rule exclusion **with
its reason** in `.github/zizmor.yml` or `.hadolint.yaml` — never a bare ignore.

There is no `Makefile`. Everything is `pyproject.toml` plus the scripts in
`scripts/`.

## The changelog

`CHANGELOG.md` is not a work log. **It is the release notes**, verbatim:
`publish.yml` hands the `[Unreleased]` section to `duplocloud/version-bump`,
which stamps a version heading on it and puts it straight into the GitHub
Release. Whatever you write is what a stranger reads.

So write for that stranger:

- **One short line per entry**, saying what someone can now do. Not a paragraph,
  not the reasoning, not what it replaced. If a line needs a "because", the
  because belongs in `AGENTS.md` or the pull request.
- **Lead with the capability and stop when the sentence is answered.** Bold the
  lead on entries someone would choose the release for; the bold is weighting, so
  a list where everything is bold has stopped weighting anything.
- **A version may open with a short preamble** framing the release as a whole.
  That is prose and the rule above does not reach it.
- **Internal work usually earns no line at all** — CI, refactors, dependency
  bumps, tests, docs. When it genuinely changes something a user would notice it
  gets one terse line under `Changed`; when it does not, the pull request takes
  the **`no changelog`** label and writes nothing. An entry nobody outside this
  repo can act on dilutes the ones that matter.
- **Only `Added` / `Changed` / `Fixed` / `Removed`**, in that order, and only the
  ones you actually have. Only a **BREAKING:** entry may run long.

Two rules about *where* you write:

- **Only ever edit `[Unreleased]`.** Every section below it carries a version
  number and is immutable — those notes shipped, and rewording them rewrites
  history somebody has already read.
- **Never add a version heading or bump a version.** Versions come from git tags
  via `setuptools_scm`, and the release flow owns them.

## Before you push

`AGENTS.md` carries the design rules that are easy to break by accident — why the
tools are a mirror and not a second vocabulary, why a provider must never store a
`Catalogue`, why an export is never rewritten in place. Read it before changing
any of those.
