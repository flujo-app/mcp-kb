# AGENTS.md

Agent context for `mcp-kb`. Read this before working in this repo.

An MCP knowledge base: skills, prompts and agent material collected from git,
WebDAV and folders into one catalogue, served as MCP resources — or as tools,
for the clients that have none. The image bakes nothing; a source is a
dependency declared in a config file and read at container **start**, into a
cache volume.

## Read first

**The design record lives in [`saga/`](saga/).** This file is the operating
manual — the rules and invariants you must not break. The saga is *why* they are
what they are, plus what has been weighed and set aside. Decisions there are
cited as `§C1.n` and are the reference for anything here that says "see the
saga".

[Chapter 1 — The Card Catalogue](saga/Chapter_1_The_Card_Catalogue.md) is the
whole design: a config file of sources (§C1.3, §C1.4), one per backend (§C1.5 —
§C1.7), the index and the `snapshot`/`live` dial (§C1.18), libraries and tags
(§C1.19), and the name (§C1.22).

Three other places, and none of them overlaps this file:

- **[The wiki](https://github.com/kubed-io/mcp-kb/wiki)** is the manual — how to
  connect a client, declare a source, deploy it, read `/health`. It is a
  submodule at `wiki/`, and three of its pages are generated from the live code.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** is the loop: setup, what CI runs, how
  the changelog is written, what is generated and what is not.
- **[README.md](README.md)** advertises. It links into the wiki and explains
  nothing.

This repo ships **an image and a Python distribution, and nothing else.** There
are no Kubernetes manifests here and no `deploy/`: a deployment is the
installer's, against whatever image tag it chooses.

## Layout

| Path | What it is |
| --- | --- |
| `kubed/mcp_kb/server.py` | `KnowledgeBase` — the composition root: cold start, the refresh loop, which snapshot is current. No tool bodies |
| `kubed/mcp_kb/main.py` | CLI and env parsing, and the `mcp-kb` console script |
| `kubed/mcp_kb/routes.py` | the plain-HTTP surface: `/health`, `/reindex`, `/openapi.yaml` |
| `kubed/mcp_kb/config.py` | the config file's model — `Config`, `Library`, `Include`, the `*Source` models, `EnvRef`, `schema()` |
| `kubed/mcp_kb/catalogue/` | what is served, and how it is found, addressed and persisted |
| ⤷ `harvest.py` | the `include` globs, and what counts as a skill, a prompt or a library file |
| ⤷ `skills.py` | the domain — `Skill`, `SkillIndex`, and the scoping rules. Imports no FastMCP |
| ⤷ `uris.py` | the `skill://` address space — `Catalogue`, the grammar |
| ⤷ `prompts.py` | `FilePrompt`, frontmatter parsing, rendering — no FastMCP |
| ⤷ `index.py` | `Index`/`SourceRecord`, the on-disk `index.json` a cold start reads |
| ⤷ `snapshot.py` | `Snapshot`, `build_snapshot` — the immutable view every request reads |
| ⤷ `refresh.py` | when a source is due to be looked at again, bounded to the shortest configured interval, and what a failure does to the last good one |
| `kubed/mcp_kb/sources/` | where bytes come from — `file.py`, `git.py`, `webdav.py`, the shared per-version `export.py`, `errors.py`, and `live.py`, because revalidation is a source concern |
| `kubed/mcp_kb/mcp/` | what an agent sees |
| ⤷ `resources.py` | the resources, which are the interface, and the middleware — `NameTheFileToRead`, `HideMirrorTools` |
| ⤷ `tools.py`, `prompts.py` | the two mirror pairs |
| ⤷ `request.py`, `scope.py` | what the current request says about itself, and the `Scope` it becomes |
| ⤷ `announce.py` | `AnnounceChanges` — telling a session its listing moved |
| ⤷ `pins.py` | `RefuseEmptyScope` — a scope that names nothing fails every request, saying what there is |
| `kubed/mcp_kb/spec/` | `builder.py`, the OpenAPI document for the HTTP surface |
| `scripts/` | `generate_openapi.py`, `generate_wiki.py`, and `requirements.py` for the image build |
| `examples/config.yaml` | the worked config the image ships — four libraries as `github://` sources |
| `config.schema.json` | `Config.model_json_schema()`, committed so an editor can validate a config live |
| `wiki/` | the GitHub wiki, as a submodule |

## Where code goes

The split follows the layout proposed in `modelcontextprotocol/python-sdk#1681`:
**MCP wiring separate from tool implementations**, with pure logic factored out
of the handlers. FastMCP has no `APIRouter` equivalent (`PrefectHQ/fastmcp#948`
closed unanswered), so each module exposes a `register(...)` the server calls.
Mounting sub-servers is the other option and is wrong here: it namespaces tool
names with a prefix, and these names are the agent's API.

- **Adding a tool** → `mcp/tools.py`. Never `server.py`. But think first: the
  tools are a *mirror* of the resources and the prompts, and one that is not
  breaks the promise the whole design rests on — that a client which knows how
  to read MCP resources already knows how to drive this server.
- **Changing the URI grammar, or what a URI resolves to** → `catalogue/uris.py`.
  Resources and tools both project from `Catalogue`, so a change there lands on
  both at once, which is the point.
- **Adding an endpoint** → `routes.py`, and its response shape in `spec/`, or
  the published contract starts lying.
- **Changing what counts as a skill, a prompt or a library file** →
  `catalogue/harvest.py` for the rule, `catalogue/uris.py` for where it applies.
- **Changing who may see one** → `catalogue/skills.py` for the rule,
  `catalogue/uris.py` for where it is applied. Every `Catalogue` method takes a
  `Scope`, so there is no method that can be called without deciding about it —
  a handler that reimplements that filter is how a pinned client ends up seeing
  another library.
- **Adding a prompt** → a file matching a source's `include.prompts` glob (or
  the convention), exposed as `<library>_<file stem>`. No code. Every
  placeholder must be a declared argument and a required argument cannot have a
  default; the server skips a file that breaks either rule, and
  `tests/test_prompts.py` loads a broken one to prove it.
- **A library referencing files outside its skills** → that source's
  `include.files`. They are served at `skill://<library>/<path>` and listed
  under `_files.md`, never indexed as skills.
- **A new flag or env var** → `main.py`, which is the whole configuration
  surface. Nothing else in the package reads `os.environ`, except `{env: NAME}`
  resolution in `config.py`, which is the one other reader — and it resolves a
  credential where it is declared so it never travels as a plain `str`.
- **Anything that changes what the catalogue holds** goes through
  `KnowledgeBase.refresh` and produces a new `Snapshot`; never mutate one.

`catalogue/` imports no FastMCP, deliberately: the catalogue is testable without
an MCP client, and `tests/test_skills.py` and `tests/test_uris.py` exercise the
scoping rules directly rather than only through a tool call.

## The surface, and why the tools are a mirror

Resources are the interface. The tools mirror them and nothing else:
`list_resources()` returns the rows `resources/list` returns,
`read_resource(uri)` takes the URI `resources/read` takes, and `list_prompts` /
`get_prompt` do the same for prompts. An agent that can drive MCP resources can
drive this server without learning anything.

Progressive disclosure lives in the address space, so adding a library adds
neither tools nor listing rows:

| call | returns | cost |
| --- | --- | --- |
| list | one index per library and per folder | ~1.9 KB for 90 skills |
| read `skill://grafana/grafana-lgtm/_index.md` | that folder's skills, as URIs | ~4k chars |
| read `skill://grafana/grafana-lgtm/loki/SKILL.md` | the instructions to follow | one file |

`<library>` is the first segment of every skill URI (the MCP Skills
extension's "server-chosen prefix"), `<folder>` mirrors the skill's directory
below its source's conventional skill root, and the last segment before the
file is always the skill's `name`. A library, a folder or a skill's own
directory is a directory address and serves nothing; see `catalogue/uris.py`'s
module docstring for the grammar in full.

Reads return **only** what was asked for. A skill body may cite
`references/FOO.md`; citing it does not fetch it. That laziness is the point —
keep it when changing this code.

A client declares it cannot read resources with `?resources=off` or
`X-MCP-Resources: off`, and only then is the resource mirror listed; prompts
work the same way with `?prompts=off`. There is no protocol signal for either —
prompts are a server capability, so a client cannot advertise using them. The
tools stay callable either way: hiding one from a listing is presentation,
refusing to run one would be a different and worse contract.

Two ways to hard-scope, both ceilings the model cannot widen past:

- **A `Scope`, per client** — `?library=` / `?tags=` or the `X-Skill-Library` /
  `X-Skill-Tags` headers. One deployment serves many narrow agents; in n8n it
  is a Header Auth credential on the MCP Client Tool node. Prefer this — a
  second copy of the server is a whole extra pod for something a header
  solves.
- **A config that lists less**, per deployment. Narrower blast radius, but a
  whole pod.

They compose: the header narrows within whatever the config loads. Header
scoping only exists inside an HTTP request, so it is inert over stdio — which is
why `tests/test_header_scope.py` runs a real uvicorn server.

`GET /health` reports the libraries, the skill and prompt counts, the
catalogue's generation and when it was built, and each source's own status and
fingerprint. `POST /reindex` returns the same payload plus `rebuilt`. Between
them they answer the three questions worth asking of a running instance: did
every configured source load, which generation is being served, and has a
refresh picked up an edit yet.

## Things that already cost someone an afternoon

- **`USER` in the Dockerfile must be numeric.** A Kubernetes pod spec with
  `runAsNonRoot: true` refuses a non-numeric user — `image has non-numeric user
  (nobody), cannot verify user is non-root` — and sits in
  `CreateContainerConfigError`. It is `USER 65534` here; an installation's own
  `runAsUser` has to match it.
- **The image build hands a venv between stages, and it has two rules.**
  `/opt/venv` must be copied to the *same absolute path* it was created at — a
  venv records that path in `pyvenv.cfg` and in every console script's shebang,
  so landing it elsewhere points it at an interpreter that is not there. And
  `COPY . .` must stay *after* the dependency install: `.git` is in the build
  context for setuptools_scm, so copying the source first invalidates the
  dependency layer on every commit, docs-only ones included. Both are asserted
  in `tests/test_packaging.py`, because the shape they replace — builder builds
  a wheel, runner installs it and every dependency a second time — reads as
  perfectly ordinary Dockerfile.
- **`kubed/` is a PEP 420 namespace and has no `__init__.py`.** That is what
  lets `kubed.mcp_kb` and the sibling `kubed.selenium_flow` install side by
  side. Adding one breaks the pair silently, in whichever environment resolves
  second; `tests/test_packaging.py` fails if one appears.
- **The prompt mirror is FastMCP's `PromptsAsTools`**, subclassed only to add
  read-only annotations. Keep it FastMCP's: a prompt is role-tagged messages,
  which a resource or a hand-written tool would flatten to text.
- **Do not reach for `ResourcesAsTools`, or back for `SkillsDirectoryProvider`.**
  Both enumerate every skill on every listing call — `resources/list` is 1.9 KB
  for this catalogue and was 77 KB before `Catalogue` replaced it with a dozen
  indexes. `SkillsDirectoryProvider` also keys a skill on its folder name alone,
  so two libraries shipping a `testing/` collapse into one and the loser
  vanishes from the server entirely. Neither failure raises anything.
- **A provider that stores a `Catalogue` serves the old generation forever** —
  read through the getter (`lambda: knowledge_base.snapshot.catalogue`), never a
  captured reference. A `Revalidator` has the same shape of bug and a nastier
  symptom: a refresh exports a source to a *new* directory, so one that outlives
  its snapshot goes on revalidating into a tree nothing is serving, and the edit
  never appears. It is built in `build_snapshot` and retired with the snapshot.
- **`live` is a per-file ETag revalidation, not fsspec's `filecache`.** Chaining
  `filecache::webdav+https://…` is the off-the-shelf answer and the wrong one:
  it stores files under hashed names in a cache directory of its own, and
  everything downstream of a source here — the harvest, the URI grammar, the
  traversal guard, `_manifest` — needs a real directory tree of `Path`s. ~80
  lines over webdav4 is the cost of keeping that invariant.
- **A fingerprint is published, so it stays small.** `build_snapshot` puts
  `record.fingerprint` straight into `/health` and `index.py` writes it to
  `index.json`. Whatever a new backend's fingerprint returns is a thing an
  operator reads and a thing rewritten to disk on every rebuild — a WebDAV
  source's whole `{path: etag}` map does not belong in either.
- **A live read blocks the whole event loop.** The resource and prompt handlers
  are `async` and `Catalogue.read` under them is synchronous, so ten concurrent
  reads of a source 300 ms away take 3.0 s, perfectly serialized, with nothing
  else on the loop running meanwhile — not another source's read and not
  `/health`. What is bounded is the *failure*: `REVALIDATE_TIMEOUT` caps one
  read and `COOLDOWN_SECONDS` stops a wedged server costing anything after the
  first. **Making the read path async — a thread for the blocking call, or an
  async WebDAV client — is the outstanding follow-up**, and it is a change to
  `mcp/resources.py`, `catalogue/uris.py` (the skill and library-file read) and
  `catalogue/prompts.py` (`FilePrompt.body()`, the live prompt read), not to
  `sources/live.py`.
- **An export is named by its version, so a rebuild of a version that has not
  moved is a rebuild over a tree being read.** That is the one case a repair
  happens at all — an export that lost files, or one a killed fetch left a temp
  file in. `Exports.ensure` publishes to the next free name (`<version>.1`) for
  exactly this reason; renaming onto `<version>` "for simplicity" brings back 2%
  empty reads under load. `tests/test_exports.py` holds it.
- **wsgidav's ETag is `inode-mtime-size`.** The test server's, not Nextcloud's,
  which is content-derived — so a test that edits a served file must change its
  *length*, or two writes in the same second are one ETag and the revalidation
  test passes for the wrong reason.
- **A glob ending in `**` is written `dir/**/*`.** Before Python 3.13 a trailing
  `**` matches directories only, so the pattern serves nothing on the oldest
  supported interpreter and everything on the newest, with no error either way.
  `harvest._globstar` normalises one in a config; in this repo's own code, write
  it out.

## House rules that apply here

- **The changelog is the release notes, not a work log.** `publish.yml` hands
  `[Unreleased]` to `duplocloud/version-bump`, which puts it straight into the
  GitHub Release with no editing pass in between. One short line per entry,
  saying what someone can now do; the reasoning belongs here in `AGENTS.md`.
  Internal work earns no line at all and takes the `no changelog` label. Only
  ever edit `[Unreleased]`, and never write a version heading — `setuptools_scm`
  reads versions from git tags and the release flow owns them. Full rules in
  [CONTRIBUTING.md](CONTRIBUTING.md#the-changelog).
- **`openapi.yaml` is generated and not committed.** It is in `.gitignore`.
  Changes go in `spec/`; `scripts/generate_openapi.py` writes a copy to lint.
- **The wiki is generated where the code knows the answer.**
  `Configuration.md`, `Tools.md` and `Endpoints.md` come from
  `scripts/generate_wiki.py`; hand-written prose for one goes in
  `wiki/notes/<page>.notes.md`. The `.notes.md` suffix is load-bearing: a GitHub
  wiki addresses a page by *basename* whatever directory it is in, so
  `notes/Tools.md` would answer to the same URL as `Tools.md` and GitHub would
  serve the fragment. `tests/test_wiki.py` fails on both drift and shadowing.
- **Run Publish once with `push: false` first.** That is a real dry run: it runs
  the tests, computes the next version, and builds both the image and the
  package without pushing any of them. A failed build after a successful tag
  strands a tag on a nonexistent image, which is exactly what the dry run buys.
- **A credential never reaches a log, an error, a path on disk, the index,
  `/health` or a served body.** A backend is config-only: no cache path, clone
  path, WebDAV URL or backend name in a served URI, a listing row, a prompt name
  or a body. A source URL carrying its own `user:token@` is refused, and the
  refusal does not quote it back.
- **Absent and out-of-scope must stay indistinguishable.** Every URI here is
  guessable by design, so knowing a skill's name must not be enough to confirm
  it exists.
- `.github/zizmor.yml` and `.hadolint.yaml` record *why* each relaxed rule is
  relaxed. An ignore without a reason does not belong in either.
- No `Makefile`. Everything is `pyproject.toml` plus the scripts in `scripts/`.
- **No auth on this server, by design.** Every skill it serves is public
  markdown already on GitHub, it has no write path, and it holds no credentials
  beyond the `{env:}` references it resolves outbound. Do not add a token
  without a concrete threat.
