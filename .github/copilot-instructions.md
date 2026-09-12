# Copilot code review — skills-mcp

## Purpose & scope

You are reviewing pull requests for a **Python MCP server** that serves
[Agent Skills](https://code.claude.com/docs/en/skills) — `SKILL.md` packages —
over HTTP, so clients that cannot read a filesystem can still use them. Skills
are **fetched at build time** from pinned upstream repos and baked into the
image; nothing is fetched at runtime. The package is `kubed.skills_mcp`; it ships
as a container image (`kubed/skills-mcp`) deployed to the `flow` namespace.

**Read these repo files first — they are the source of truth, and you should back
your comments with them:**

- **`AGENTS.md`** — the architectural non-negotiables and the reasoning behind
  them. This is the most important file in the repo for a reviewer.
- **`kubed/skills_mcp/uris.py`** — the `skill://` grammar, which is the API.
- **`skills.toml`** — the pack manifest, and the only file you edit to add a pack.

Prefer these over assumptions. When a convention is undocumented, the sibling
repo `kubed-io/selenium-flow` sets the house style for CI, release flow and the
resources/tools split.

## The principle that dominates every review: the tools are a mirror

MCP has a primitive for material an agent reads, and it is the **resource**. So
resources are the interface here, and the two tools exist only because some
clients — n8n above all — do not implement resources at all.

That makes the mirror a contract, not a convenience:

- **`list_resources()` returns the rows `resources/list` returns.
  `read_resource(uri)` takes the URI `resources/read` takes.** A client that can
  drive MCP resources can drive this server without learning anything. A PR that
  makes a tool diverge from its resource — a different shape, an extra argument,
  a filter only one half applies — breaks the one promise the design rests on.
- **A third tool that is not a mirror is a finding.** Say where it belongs: in
  the address space, as a URI that resolves to something.
- **`uris.py` is the only place behaviour lives.** `resources.py` and `tools.py`
  are thin projections of `Catalogue`. A PR that puts resolution logic in a
  projection is a finding — a rule applied in one of them is a rule the other
  half does not have.
- Operational routes — `/health` — are HTTP-only on purpose. They describe the
  server rather than serving a skill.

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

1. **Scope enforcement** — `X-Skill-Pack` pins a client to one pack or group, and
   it is a ceiling the model cannot widen past. Every `Catalogue` method takes
   `pinned` for that reason: there is no method that can be called without
   deciding about the scope. A new path that reads content without threading it
   is a finding even when it looks correct, because **filtering a listing is not
   enough** — every URI here is guessable by design, so an unfiltered read is
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
7. **Tests** — a `kubed/` change should carry a test in `tests/`. `test_uris.py`
   covers the grammar and the scope directly, without an MCP client; prefer a
   test there over one that can only reach the rule through a tool call.

## Python conventions specific to this repo

- **Type hints in `tools.py` ARE the tool schema.** FastMCP builds the JSON
  schema from the signature, so a missing or loose annotation ships a worse tool.
  `param: str = None` instead of `param: str | None = None`, a bare `dict`, an
  untyped `**kwargs` — all are real findings here.
- **Docstrings in `tools.py` are prompt, not documentation.** They are read by a
  model choosing a tool. Review them for that reader: what the tool does, when to
  reach for it, what the arguments mean. The `read_resource` docstring carries
  the URI grammar because that is where a model will look for it.
- **`skills.py` imports no FastMCP, deliberately.** The catalogue is testable
  without an MCP client. A PR that reaches for a FastMCP type in it is a finding.
- **Reads are lazy and must stay that way.** A skill body may cite
  `references/FOO.md`; citing it does not fetch it. A change that eagerly walks a
  skill's files to answer a read is a regression in the thing this server is for.
- **`main.py` is the whole configuration surface.** Nothing else in the package
  reads `os.environ`. Per-request configuration goes in `request.py`.
- Prefer `pathlib` over `os.path`, f-strings over `%`/`.format`, and
  `from __future__ import annotations` at the top of new modules to match the
  existing files.

## Project non-negotiables — do not approve changes that break these

- **`skills/` is a build artifact and is gitignored.** Upstream markdown is never
  vendored. A PR that commits a fetched skill is wrong; the change belongs in
  `skills.toml` as a one-line `ref` bump.
- **Pin a SHA, not a branch.** None of the upstreams tag releases, so a branch
  ref makes the image irreproducible and a pack can change under you.
- **`path` must be the directory that *contains* skill folders, never the repo
  root.** `grafana/skills` ships a `template/SKILL.md` at the top level that would
  otherwise be served as a skill named "template".
- **Do not reach for `ResourcesAsTools`, or back for `SkillsDirectoryProvider`.**
  Both enumerate every skill on every listing call, and the second also keys a
  skill on its folder name alone, so two packs shipping the same name collapse
  into one and the loser vanishes. Neither failure raises anything.
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
- **`test.yml` runs one interpreter (3.14) on a PR and the full 3.10–3.14 matrix
  on main and on release.** `Test (3.14)` is a required status check.
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

- **Serving arbitrary markdown from the image is the product.** The skills are
  public upstream content baked in at build time. Don't raise it as untrusted
  input to the server; it is the payload.
- **There is no authentication, deliberately.** Every skill served is public
  markdown already on GitHub, the server has no write path, and it holds no
  credentials. Don't ask for a token without a concrete threat.
- **Absent and out-of-scope returning the same thing is deliberate**, not a lost
  error message. See priority 1.
- **The pack-qualified URI is not redundant.** `skill://<pack>/<skill>` costs a
  segment and buys a namespace that cannot collide; the unqualified form is what
  silently dropped a skill.
- **A hidden tool that is still callable is deliberate.** Hiding a tool from a
  listing is presentation; refusing to run one a client already knows about would
  be a different and worse contract.
- **Actions are pinned to release tags, not commit hashes.** This is a recorded
  decision — see the `unpinned-uses` block in `.github/zizmor.yml`.
- **A terse changelog entry is correct, not lazy.** One line is the house style.
