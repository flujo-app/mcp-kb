# Copilot code review — mcp-kb

## Purpose & scope

You are reviewing pull requests for a **Python MCP server**: an MCP knowledge
base that collects [Agent Skills](https://code.claude.com/docs/en/skills),
prompts and agent material from git, WebDAV and folders into one catalogue and
serves it as MCP resources — or as tools, for the clients that have none. The
image bakes nothing: a source is a dependency declared in a config file and
fetched at container start, into a cache volume. The package is `kubed.mcp_kb`;
it ships as a container image (`kubed/mcp-kb`) and a Python distribution, with no
deployment manifest of its own — that lives with whoever installs it.

**Read these repo files first — they are the source of truth, and you should back
your comments with them:**

- **`AGENTS.md`** — the architectural non-negotiables and the reasoning behind
  them. This is the most important file in the repo for a reviewer.
- **`CONTRIBUTING.md`** — the layout, what is generated and what is not, and
  what CI will say about a change.
- **`kubed/mcp_kb/catalogue/uris.py`** — the `skill://` grammar, which is the API.
- **`examples/config.yaml`** — the worked example, and the file you edit to add
  a library to it.

Prefer these over assumptions. When a convention is undocumented, the sibling
repo `kubed-io/selenium-flow` sets the house style for CI, release flow and the
resources/tools split.

## The layout

| Path | Holds |
|---|---|
| `kubed/mcp_kb/` | `server.py` composes it and owns which snapshot is current, `main.py` starts it, `routes.py` is the plain-HTTP surface, `config.py` is the config model and the published schema |
| `kubed/mcp_kb/catalogue/` | what is served: `harvest.py` decides what counts, `skills.py` is the domain, `uris.py` is the `skill://` address space, `prompts.py` parses a prompt file with no FastMCP in it, `index.py` persists it, `snapshot.py` builds one immutable view, `refresh.py` decides when to look again |
| `kubed/mcp_kb/sources/` | where bytes come from: `file.py`, `git.py`, `webdav.py`, the shared `export.py`, `errors.py`, and `live.py` because revalidation is a source concern |
| `kubed/mcp_kb/mcp/` | what an agent sees: `resources.py` is the interface, `tools.py` and `prompts.py` the mirrors, `request.py` and `scope.py` the per-request scope, `announce.py` the list-changed notification |
| `kubed/mcp_kb/spec/` | the OpenAPI document for the HTTP surface |
| `scripts/` | `generate_openapi.py`, `generate_wiki.py`, and `requirements.py` for the image build |
| `examples/` | `config.yaml`, the worked config the image ships |
| `wiki/` | the GitHub wiki, as a submodule — three pages generated, the rest hand-written |

`kubed/` is a **PEP 420 namespace package and has no `__init__.py`**, which is
what lets `kubed.mcp_kb` and `kubed.selenium_flow` install side by side. A PR
that adds one is a finding, and `tests/test_packaging.py` fails on it.

## The principle that dominates every review: the tools are a mirror

MCP has a primitive for material an agent reads, and it is the **resource**, and
one for a template a person picks, and it is the **prompt**. Both are the
interface here. The four tools exist only because some clients — n8n above all —
implement neither.

That makes the mirror a contract, not a convenience:

- **`list_resources()` returns the rows `resources/list` returns.
  `read_resource(uri)` takes the URI `resources/read` takes**, and `list_prompts`
  / `get_prompt` do the same for `prompts/list` and `prompts/get`. A client that
  can drive MCP resources can drive this server without learning anything. A PR
  that makes a tool diverge from what it mirrors — a different shape, an extra
  argument, a filter only one half applies — breaks the one promise the design
  rests on.
- **A fifth tool that is not a mirror is a finding.** Say where it belongs: in
  the address space, as a URI that resolves to something.
- **`catalogue/uris.py` is the only place behaviour lives.** `mcp/resources.py`
  and `mcp/tools.py` are thin projections of `Catalogue`. A PR that puts
  resolution logic in a projection is a finding — a rule applied in one of them
  is a rule the other half does not have.
- **The prompt mirror is FastMCP's own `PromptsAsTools`**, subclassed only for
  read-only annotations. A hand-written replacement flattens role-tagged
  messages to text, which is the thing that makes a prompt a prompt.
- Operational routes — `/health`, `/reindex`, `/openapi.yaml` — are HTTP-only on
  purpose. They describe or rebuild the server rather than serving a skill.

## Signal over volume — the most important rule for this reviewer

Fewer, higher-value comments beat exhaustive nitpicking. A review with 3 real
findings is better than one with 15 where 12 are cosmetic. Noise trains the team
to ignore you.

- **Worth a comment:** correctness, scope enforcement, and the mirror rule above —
  always.
- **Usually skip:** a wording tweak, a slightly-stale comment, a missing type on
  an internal helper — unless the file is otherwise clean or it is egregious.
- **Verify a bug is real before flagging it.** Trace the guards in the *same
  function* and confirm the failure path is reachable. Don't file speculative
  "this could break if X" without a concrete path to X.
- **Assume the library is correct before calling something unsafe.** If FastMCP
  or Starlette plausibly already handles the concern, assume it does unless the
  code shows otherwise.

## Review priorities (highest first)

1. **Scope enforcement** — a `Scope` (`?library=`, `?tags=`, or the
   `X-Skill-Library` / `X-Skill-Tags` headers) narrows a client,
   and it is a ceiling the model cannot widen past. Every `Catalogue`, `SkillIndex`
   and `PromptProvider` read takes a `Scope` for that reason: there is no method
   that can be called without deciding about the scope. A new path that reads
   content without threading it is a finding even when it looks correct, because
   **filtering a listing is not enough** — every URI here is guessable by design, so an unfiltered read is
   reachable by anyone who can spell it. Absent and out-of-scope must stay
   indistinguishable: knowing a skill's name must not confirm it exists.
2. **The mirror rule** — the section above.
3. **Tool schema quality** — see below. On this project a weak schema is a
   functional defect, not a style nit.
4. **Path safety** — a model supplies the path inside a URI. Reads resolve before
   comparing, so `../` and a symlink pointing out of a skill are refused. A new
   read that skips that is a finding.
5. **Correctness** — error paths and edge cases; re-derive test expectations from
   the documented behaviour, not from the current implementation.
6. **Dead code & simplification** — unused code and imports, redundant
   abstractions.
7. **Tests** — a `kubed/mcp_kb/` change should carry a test in `tests/`.
   `test_uris.py` and `test_skills.py` cover the grammar and the scope directly,
   without an MCP client; prefer a test there over one that can only reach the
   rule through a tool call.

## Python conventions specific to this repo

- **Type hints in `mcp/tools.py` ARE the tool schema.** FastMCP builds the JSON
  schema from the signature, so a missing or loose annotation ships a worse tool.
  `param: str = None` instead of `param: str | None = None`, a bare `dict`, an
  untyped `**kwargs` — all are real findings here.
- **Docstrings in `mcp/tools.py` are prompt, not documentation.** They are read by a
  model choosing a tool. Review them for that reader: what the tool does, when to
  reach for it, what the arguments mean. The `read_resource` docstring carries
  the URI grammar because that is where a model will look for it.
- **`catalogue/` imports no FastMCP, deliberately.** The catalogue is testable
  without an MCP client. A PR that reaches for a FastMCP type in it is a finding.
- **Reads are lazy and must stay that way.** A skill body may cite
  `references/FOO.md`; citing it does not fetch it. A change that eagerly walks a
  skill's files to answer a read is a regression in the thing this server is for.
- **`main.py` is the whole configuration surface.** Nothing else in the package
  reads `os.environ`, except `{env: NAME}` resolution in `config.py`, which
  resolves a credential where it is declared so it never travels as a plain
  `str`. Per-request configuration goes in `mcp/request.py`.
- **A glob ending in `**` is written `dir/**/*`.** Before Python 3.13 a trailing
  `**` matches directories only, so the pattern finds nothing on 3.11 and
  everything on 3.14, silently.
- Prefer `pathlib` over `os.path`, f-strings over `%`/`.format`, and
  `from __future__ import annotations` at the top of new modules to match the
  existing files.

## Project non-negotiables — do not approve changes that break these

- **A fetched source is never committed.** It is cache, materialised at start
  under `CACHE_DIR`. A PR that commits fetched skill content is wrong; the
  change belongs in a config file as a one-line `ref` bump.
- **Pin a SHA, not a branch, unless a `refresh` is declared alongside it.** None
  of the upstreams in `examples/config.yaml` tag releases, so an unpinned,
  unrefreshed source makes the image irreproducible and a library can change
  under you with no signal that it did.
- **`include` globs must reach *into* the directory that contains skill
  folders, never the repo root.** `grafana/skills` ships a `template/SKILL.md`
  at the top level that would otherwise be served as a skill named "template".
- **Do not reach for `ResourcesAsTools`, or back for `SkillsDirectoryProvider`.**
  Both enumerate every skill on every listing call, and the second also keys a
  skill on its folder name alone, so two libraries shipping the same name
  collapse into one and the loser vanishes. Neither failure raises anything.
- **`openapi.yaml` is a generated build artifact and is gitignored.** Changes go
  in `spec/`. A PR that commits the generated file, or edits it directly, is
  wrong.
- **The wiki is generated** where the code knows the answer.
  `Configuration.md`, `Tools.md` and `Endpoints.md` come from
  `scripts/generate_wiki.py`; hand-written prose for one belongs in
  `wiki/notes/<page>.notes.md`, and the `.notes.md` suffix is load-bearing — a
  GitHub wiki addresses a page by basename, so `notes/Tools.md` would shadow
  `Tools.md`. `tests/test_wiki.py` fails on both drift and shadowing.
- **A credential never reaches a log, an error message, a path on disk, the
  index, `/health` or a served body**, and no cache path, clone path, WebDAV URL
  or backend name reaches a served URI, a listing row or a prompt name.
- **Versions come from git via setuptools_scm.** Never ask for a hand-written
  version bump; the release flow owns versions.

## CI conventions

Workflow changes are governed by
[github-workflows.instructions.md](./instructions/github-workflows.instructions.md),
YAML style by [yaml.instructions.md](./instructions/yaml.instructions.md), and
Python by [python.instructions.md](./instructions/python.instructions.md). The
shape of the pipeline itself:

- **The image is not built on pull requests** — deliberately. Don't ask for it
  back; `quality.yml`, `package.yml` and `test.yml` cover the part that is ours.
- **`test.yml` runs both ends of the range (3.11 and 3.14) on a PR and the full
  3.11–3.14 matrix on main and on release.** `Test (3.14)` is a required status
  check. The old end is on the PR because that is where every version break
  here has been: a trailing `**` glob, and a `pygit2` floor without the API.
- **Required checks must never be path-filtered.** A path-filtered required check
  never reports on a PR that misses the filter, and the PR can then never merge.
- **Every quality gate has a config file with reasons**: `.github/zizmor.yml` and
  `.hadolint.yaml` record *why* each rule is relaxed. A PR that adds an ignore
  without a reason is a finding.

## Changelog entries are release notes — review them as such

`CHANGELOG.md`'s `[Unreleased]` section is handed verbatim to the release. What
a PR writes there is what a stranger reads on the GitHub Release, so it is worth
a comment when it is wrong — and the failure is almost always the same one:

- **A paragraph instead of a line.** Entries explain the reasoning, name what
  they replaced, or narrate the investigation. Say so and propose the one-line
  version. The reasoning belongs in `AGENTS.md` or the PR description. Only
  **BREAKING:** may stretch.
- **An entry for internal work.** CI changes, refactors, dependency bumps, test
  passes and doc edits earn no entry — the `no changelog` label is the intended
  answer. One terse line under `Changed` is right only when a user would
  genuinely notice.
- **An edit to a released section.** Everything below `[Unreleased]` is
  immutable. Flag any diff that touches it.
- **A hand-written version heading or version bump.** Versions come from git tags
  via `setuptools_scm`; the release flow owns them.

Do *not* ask for an entry that `pr.yml` did not — and do not ask for a version
heading.

## What not to flag (settled — these are the recurring false positives)

- **Serving arbitrary markdown is the product.** The skills are public upstream
  content, fetched at start from sources the config names. Don't raise it as
  untrusted input to the server; it is the payload.
- **There is no authentication, deliberately.** Every skill served is public
  markdown already on GitHub, the server has no write path, and it holds no
  credentials. Don't ask for a token without a concrete threat.
- **Absent and out-of-scope returning the same thing is deliberate**, not a lost
  error message. See priority 1.
- **The library-qualified URI is not redundant.**
  `skill://<library>/<folder>/<skill>/SKILL.md` costs a segment and buys a
  namespace that cannot collide; a bare name, or a folder name with no library
  in front of it, is what silently dropped or merged a skill.
- **A hidden tool that is still callable is deliberate.** Hiding a tool from a
  listing is presentation; refusing to run one a client already knows about would
  be a different and worse contract.
- **Actions are pinned to release tags, not commit hashes.** This is a recorded
  decision — see the `unpinned-uses` block in `.github/zizmor.yml`.
- **A terse changelog entry is correct, not lazy.** One line is the house style.
