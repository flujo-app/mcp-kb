# mcp-kb Milestone Plan — rename, restructure, document, first release

> **For agentic workers:** executed with superpowers:subagent-driven-development,
> task by task, on the branch `mcp-kb`. One PR at the end.

**Goal:** the project becomes `mcp-kb` — package `kubed.mcp_kb`, image
`kubed/mcp-kb`, command `mcp-kb` — organised into subpackages the way the
sibling `selenium-flow` is, with generated documentation in a wiki submodule,
a README that advertises, a CHANGELOG shaped as a first release, and a publish
flow that matches the sibling's. No behaviour changes except where a task says.

**Spec:** `saga/Chapter_1_The_Card_Catalogue.md` is the design record. The
sibling `/projects/modules/selenium-flow` is the reference for *shape*: its
`pyproject.toml`, `CONTRIBUTING.md`, `README.md`, `CHANGELOG.md` (the
`[0.0.2]` section is the first-release shape), `.github/workflows/{publish,wiki}.yml`,
`scripts/generate_{openapi,wiki}.py`, `wiki/`.

**Tech stack:** Python ≥3.11, FastMCP 4.0.x, pydantic, setuptools_scm, ruff,
pytest. Toolchain in this pod: `PYTHONPATH=/home/coder/.cache/mcp-school-pylib`
(no venv); run `python3 -m pytest -q` and `python3 -m ruff check .` from the
repo root with that set.

## Global Constraints

- **The suite is 369 passed, 1 skipped at the start** and must stay green after
  every task; a task may add tests, never weaken one. Existing assertions are
  frozen in refactor tasks — an import line may change, an expectation may not.
- Ruff clean at 88 columns. A glob ending in `**` is written `dir/**/*` in code.
- Nothing outside `main.py` and `EnvRef.resolve` reads `os.environ`.
- Credentials never reach a log, an error message, a path on disk, the index,
  `/health` or a served body. A backend is config-only: no cache path, clone
  path, WebDAV URL or backend name in a served URI, listing row, prompt name or
  body.
- Every listing and read takes a `Scope` and decides about it.
- Every new test is proved non-vacuous: break the thing it guards, watch it
  fail, restore. Say so in the report.
- Commit trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- No implementer dispatches subagents. Reports go to the file named in the
  dispatch; the final message is status, commits, one-line test summary,
  concerns.
- Docs are for the present tense. History and superseded decisions belong in
  the saga, as a record. Comments earn their lines: keep the non-obvious why,
  cut narration. READMEs advertise and link; AGENTS.md and the wiki explain.
- The `kubed` directory is a PEP 420 namespace: **there is no `kubed/__init__.py`**
  in this repo, and a test asserts it. That is what lets `kubed.selenium_flow`
  and `kubed.mcp_kb` install side by side.

## Names, exactly

| Was | Is |
|---|---|
| repo `kubed-io/mcp-school` | `kubed-io/mcp-kb` (done) |
| package `mcp_school` | `kubed.mcp_kb` |
| distribution `mcp-school` | `kubed-mcp-kb` |
| console script `mcp-school` | `mcp-kb` |
| image `kubed/mcp-school` | `kubed/mcp-kb` |
| `/etc/mcp-school/config.yaml`, `/var/cache/mcp-school` | `/etc/mcp-kb/config.yaml`, `/var/cache/mcp-kb` |
| class `School` | `KnowledgeBase` |
| FastMCP server name `mcp-school` | `mcp-kb` |
| `X-Skill-*` headers, `skill://` scheme, `CONFIG`/`CACHE_DIR` env | unchanged |

`skills-mcp` / `skills_mcp` / `skillsmcp` survive only in the saga and the
CHANGELOG history. Everywhere else they are gone.

## Target layout

```
kubed/mcp_kb/
  __init__.py  __main__.py  main.py  server.py  routes.py      the app
  config.py                                                    the config model, EnvRef, schema()
  catalogue/   harvest.py  skills.py  uris.py  index.py  snapshot.py
  sources/     errors.py  export.py  file.py  git.py  webdav.py  live.py
  mcp/         request.py  scope.py  resources.py  prompts.py  tools.py
  spec/        builder.py                                      the OpenAPI document for the HTTP surface
```

`catalogue/` is what is served and how it is found, addressed and persisted.
`sources/` is where bytes come from, and `live.py` moves in because
revalidation is a source concern. `mcp/` is what an agent sees, and the
per-request scope that shapes it. `server.py` keeps `KnowledgeBase` and the
refresh loop; `AnnounceChanges` moves to `mcp/announce.py`.

---

### Task 1: the rename

**Files:** everything. `git mv mcp_school kubed/mcp_kb`; no `kubed/__init__.py`.

- `pyproject.toml`: `name = "kubed-mcp-kb"`, description reworded for a
  knowledge base of agent material (keep it one sentence), `[project.scripts]`
  `mcp-kb = "kubed.mcp_kb.main:main"`, `[tool.setuptools] packages`
  `["kubed.mcp_kb", "kubed.mcp_kb.sources"]` (Task 2 extends it), URLs to
  `kubed-io/mcp-kb`.
- Every `mcp_school` import and string → `kubed.mcp_kb`; `School` →
  `KnowledgeBase`; FastMCP name and `INSTRUCTIONS` wording → `mcp-kb`;
  `Dockerfile`, `docker-compose.yaml`, `.github/workflows/*.yml`,
  `.github/copilot-instructions.md`, `.github/instructions/*.md`, `AGENTS.md`,
  `README.md` (mechanical replacement only — Task 6 rewrites it), `examples/`,
  `config.schema.json` (regenerate with `python3 -m kubed.mcp_kb schema`),
  every test.
- `tests/test_packaging.py`: adapt the packages guard to `kubed/mcp_kb/**`,
  and add `test_kubed_is_a_namespace_package` asserting no `kubed/__init__.py`
  exists and that the wheel contains none.
- Prove side-by-side installation once, in the report: build this wheel and
  the sibling's (`/projects/modules/selenium-flow`), `pip install --target`
  both into one directory, and import `kubed.mcp_kb.server` and
  `kubed.selenium_flow.server` from it in one interpreter.
- Do NOT touch `saga/` or the `CHANGELOG.md` history — only `[Unreleased]`'s
  wording where it names the old package (Task 6 rewrites that section).

Commit: `rename: mcp-school becomes mcp-kb, packaged as kubed.mcp_kb`

---

### Task 2: subpackages

**Files:** the moves in *Target layout*; `pyproject.toml` packages list; every
importer; `tests/test_packaging.py` guard passes.

Refactor only: no signature, behaviour or test-expectation change. Inside a
subpackage siblings import as `from . import x`; across subpackages, absolute
within the package (`from ..catalogue import uris`). `mcp/announce.py` gets
`AnnounceChanges` and `_can_remember` out of `server.py`. Module docstrings
move with their modules and are corrected where they name a path.

Commit: `refactor: catalogue, sources and mcp subpackages`

---

### Task 3: the quality pass

**Files:** as found. A capable model reads the whole package after Task 2 and
makes the cleanups that are clearly right, each as its own commit with a
one-line reason. Candidates the plan already knows about:

- `server.py` (491 lines) — `refresh`, `_due`, `_keep_last_good`,
  `_same_failure` and the lifespan loop are a refresh policy; consider
  `catalogue/refresh.py` if the seam is clean. `KnowledgeBase` stays the
  composition root.
- Three near-identical "does the scope admit this pack" checks in `uris.py`
  (`_content`, `_pack_files`, listing) — one helper.
- `request.py`'s three header-or-param readers — one `_read(header, param)`.
- Anything duplicated between `sources/git.py` and `sources/webdav.py` beyond
  what `export.py` already shares.
- Docstrings and comments that narrate history ("used to", "before this
  change", epic numbers, review findings) — cut or move to the saga.

Hard rule: the suite stays frozen; a cleanup that needs an assertion changed is
not a cleanup. Bounded: stop at diminishing returns, report what was left.

Commit per change.

---

### Task 4: the OpenAPI document

**Files:** create `kubed/mcp_kb/spec/__init__.py`, `spec/builder.py`,
`scripts/generate_openapi.py`, `tests/test_openapi.py`; modify `routes.py`,
`.github/workflows/quality.yml`, `pyproject.toml` (`openapi-spec-validator` in
`test`), `.gitignore` (`openapi.yaml` is a build artifact, not committed).

The HTTP surface is small — `GET /health`, `POST /reindex` — and so is the
document: OpenAPI 3.1, `info` from `pyproject`, one server entry per the
sibling's pattern, response schemas written from what `routes.py` actually
returns (the `/health` payload shape is in `routes.py`; describe every field).
Served at `GET /openapi.yaml` with no credentials, like the sibling.
`quality.yml` gains the generate + `npx @redocly/cli@2 lint` steps the
sibling has. `tests/test_openapi.py` validates the built spec and asserts the
served copy equals the built one.

Commit: `feat: the HTTP surface as OpenAPI 3.1 at /openapi.yaml`

---

### Task 5: the wiki

**Files:** `wiki/` (the submodule — commit inside it and record the pointer),
`scripts/generate_wiki.py`, `.github/workflows/wiki.yml`, `tests/test_wiki.py`,
`CONTRIBUTING.md`, `pyproject.toml` if needed.

Generated pages, from the live objects, with a banner and a folded
`wiki/notes/<page>.notes.md` where one exists (copy the sibling's mechanism,
including the `.notes.md` suffix and the shadowing test):

- `Configuration.md` — from `config.schema()`: one section per model
  (`Config`, `Library`, `Include`, each `*Source`, `BasicAuth`, `EnvRef`),
  every field with type, default and description, and the URL scheme table.
- `Tools.md` — from `KnowledgeBase(...).mcp.list_tools(run_middleware=False)`:
  one section per tool with its description, input schema and annotations,
  and the header/param that reveals it.
- `Endpoints.md` — from the OpenAPI document of Task 4.

Hand-written pages (present tense, no history): `Home.md`, `Installing.md`
(Claude Code, n8n MCP Client Tool, VS Code, curl — with the `?resources=off`,
`?prompts=off`, `?library=`, `?tags=` and header forms), `Deployment.md`
(Docker, Kubernetes: ConfigMap-mounted config and prompts, emptyDir cache
with `fsGroup`, startup probe, `/health` semantics), `Sources.md` (file, git,
WebDAV; include globs incl. the directory form; `refresh`; `cache: live`;
pinned vs floating refs), `Skills.md` (the `skill://` grammar, indexes,
`_manifest`, `_files`), `Prompts.md` (the file format, arguments, slash
commands, the tool mirror), `Scoping.md` (library, tags, headers, ceilings),
`Operations.md` (`/health`, `/reindex`, `index.json`, the cache directory,
what a stale source means), `_Sidebar.md`, `_Footer.md`. Replace the
placeholder `Home.md`.

`wiki.yml` is the sibling's, with paths for this repo (`kubed/**`, `wiki`,
`scripts/generate_wiki.py`). `test_wiki.py` fails when committed pages differ
from the generator's output, skipping when the submodule is absent.
`CONTRIBUTING.md` is written on the sibling's shape for this repo.

Commits: `docs: the wiki, generated where the code knows the answer` and the
submodule pointer bump.

---

### Task 6: README, AGENTS, CHANGELOG, saga, publish

**Files:** `README.md`, `AGENTS.md`, `CHANGELOG.md`, `saga/Chapter_1_The_Card_Catalogue.md`,
`.github/copilot-instructions.md`, `.github/workflows/publish.yml`,
`examples/config.yaml`.

- **README** — rewritten from scratch on the sibling's and the `nextcloud-*`
  READMEs' shape: a one-line pitch, badges (Test, Quality, Image, Wiki,
  License, Docker, FastMCP), "The whole idea, in one breath" with a small
  diagram (sources → mcp-kb → clients), then short sections that each link
  into the wiki for depth: the address space, resources first / tools as a
  mirror, scoping, sources, prompts, running it (`docker run` with the
  example config), configuration table (env/flag/default), contributing,
  references, licence. It advertises; it does not explain.
- **AGENTS.md** — refreshed for the new layout and names; the "cost someone
  an afternoon" list kept, present tense, history moved to the saga.
- **CHANGELOG** — `[Unreleased]` rewritten as a first release: a short
  preamble, then `### Added` only, one short line per capability a user can
  now rely on. No Fixed, no Changed, no Breaking: nothing has ever shipped.
  The file's own header comment is updated to the sibling's wording.
- **Saga** — a new `### §C1.22` recording the rename to `mcp-kb` (knowledge
  base: clearer, two letters), the `kubed.mcp_kb` namespace, and the
  restructure; the old names stay in the earlier sections as history. Fix
  any present-tense statement elsewhere in the chapter that is now false
  by marking it superseded, not by rewriting it.
- **publish.yml** — aligned with the sibling: the `wiki` job, the release
  body composed to a file with image/pip instructions, release notes carried
  as an artifact (the secret-masking reason is in the sibling's comments),
  `kubed/mcp-kb`. Keep this repo's GCP-secret auth path for the App token.
- **copilot-instructions.md** — layout table and review priorities updated.

Commit: `docs: README, AGENTS, a first-release changelog, and the saga's rename note`

---

### Task 7: final review and fix wave

Whole-branch review on the most capable model against this plan and the
constraints; one fix dispatch; one scoped re-review; residuals parked with
rulings in the ledger.

## Self-review

- Names table ↔ Task 1 ↔ Task 6 publish/README: consistent.
- Layout ↔ Task 2 ↔ Task 5 wiki.yml paths (`kubed/**`): consistent.
- Task 4 produces the spec Task 5's `Endpoints.md` consumes: sequential.
- Task 3 runs before Tasks 4–6 so docs describe the final shape.
