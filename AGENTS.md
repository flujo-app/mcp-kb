# Working on mcp-school

An MCP server that serves Agent Skills — `SKILL.md` packages — over HTTP so
clients that cannot read a filesystem (n8n agents, above all) can still use
them. The image bakes nothing: a source is a dependency declared in a config
file, and it is cloned or read at container **start**, into a cache volume.

## Layout

| Path | What it is |
| --- | --- |
| `mcp_school/config.py` | the config file's schema — `Config`, `load_config`, `Library`, `FileSource`, `GitSource`, `WebdavSource` |
| `mcp_school/harvest.py` | turns a source's `include` globs into skill dirs, prompt files and pack files |
| `mcp_school/sources/` | turns a config source into a local directory — `file://`, `git+…`/`github://`, `webdav+…` — and fingerprints it, to detect a changed one; `export.py` is the per-version materialise git and WebDAV share |
| `mcp_school/index.py` | `Index`/`SourceRecord`, the on-disk `index.json` a cold start reads instead of re-harvesting |
| `scripts/requirements.py` | prints the dependency list out of `pyproject.toml` for the image build |
| `examples/config.yaml` | the worked example the image ships — the four packs as `github://` sources |
| `config.schema.json` | `Config.model_json_schema()`, committed so an editor can validate a config live |
| `mcp_school/skills.py` | the catalogue — `Skill`, loading, and `SkillIndex` |
| `mcp_school/uris.py` | the `skill://` address space — `Catalogue`, the grammar |
| `mcp_school/resources.py` | the resources, and the mirror-hiding middleware |
| `mcp_school/tools.py` | the two mirror tools |
| `mcp_school/request.py` | what the current request says about itself |
| `mcp_school/live.py` | `cache: live` — the `Revalidator` a read goes through before it serves a file |
| `mcp_school/prompts.py` | loads, renders and scopes the prompts |
| `mcp_school/routes.py` | plain HTTP endpoints (`/health`, `/reindex`) |
| `mcp_school/snapshot.py` | `Snapshot`, `build_snapshot` — the immutable view of the catalogue every request reads |
| `mcp_school/server.py` | `School` — wiring, cold start, refresh, no tool bodies |
| `mcp_school/main.py` | CLI and env parsing; the only file reading `os.environ` |

## Adding a source

`examples/config.yaml` — the worked example the image ships — is what you edit
to try one. A source is a dependency, declared like one:

```yaml
sources:
- name: penpot
  url: github://penpot/penpot-ai-kit
  ref: c63d8e3717323fad859e794848e5a602b155a7ec
  include:
    skills: ["skills/*/SKILL.md"]
    files: ["shared/**/*", "workflows/**/*"]
```

`url` picks the backend: `file:///path` (absolute, no host) is served in
place; `git+https://`, `git+http://` and `git+file://` clone bare and shallow;
`github://org/repo` is shorthand for `git+https://github.com/org/repo.git`;
`webdav+https://` and `webdav+http://` copy a WebDAV folder, which needs an
`auth` (a Nextcloud app password, as `{env: NAME}`) and takes a
`cache: snapshot|live` dial — see the README for what `live` does and does not
buy.
`ref` is a branch, tag or commit — a git source with no `ref` tracks the
remote's default branch, and one with no `refresh` is read once at boot and
never rebuilt until the process restarts. `subdirectory` narrows a git source
to a path within the clone; `include` globs (see below) can reach the same
effect without it, which is what the shipped example does.

`include` is globs per kind, relative to the source root; setting one
*replaces* its convention rather than appending to it (`harvest.DEFAULTS` is
what applies when `include` is left out entirely), and `[]` turns that kind
off. A source joins a library — its own name unless it names one with
`library:`, which is how several sources present as one grouping. A pack that
references shared material outside its skills — penpot does, 190 times —
names those directories in `include.files`, never `skills`, so they are served
at `skill://<pack>/<path>` rather than indexed as skills themselves.
`config.schema.json` is `Config.model_json_schema()`; add a `#
yaml-language-server: $schema=./config.schema.json` modeline to a config file
for an editor to validate it live, and regenerate the committed schema with
`mcp-school schema > config.schema.json` after touching `config.py` —
`tests/test_schema.py` fails when the two drift.

Then verify locally before pushing:

```bash
python3 -m pytest -q
mcp-school --config examples/config.yaml --cache-dir /tmp/mcp-school-cache \
  --transport http --port 18000
```

Then, from another shell, `curl -s localhost:18000/health` to confirm every
source is `ok`. A git source needs the network for that first run; a pinned
commit needs it once, to clone, and never again.

Nesting depth does **not** matter. `harvest.skill_dirs` walks for `SKILL.md`
through the `include` globs, so a flat source (`<pack>/<skill>/SKILL.md`,
n8n) and a nested one (`<pack>/<group>/<skill>/SKILL.md`, grafana) both work.
`harvest.group_of` turns the directory *containing* a skill into its `group`,
which is why grafana has seven selectable groups and n8n has none worth
naming.

## Bumping a pinned ref

Edit `ref` in the config and restart — a git source resolves it fresh on the
next cold start, and the cache under the old commit is simply superseded. A
source tracking a floating branch with a `refresh` interval needs no bump at
all; it repins itself on its own schedule. Either way this is a config change,
never a code change, and it ships however the config gets to the running
container — this repo has no opinion on that path.

## Shipping a change

One workflow. It does not run on a push to main.

**`🧬 Publish Version`** (`workflow_dispatch`) — four jobs:

```
test    → the full 3.11 → 3.14 matrix; gates everything below
version → rolls CHANGELOG, commits + tags main
image   → checks out that tag, builds and pushes kubed/mcp-school:vX.Y.Z
package → checks out that tag, builds the sdist + wheel as a GHA artifact
release → downloads that artifact and cuts the GitHub Release
```

Run it once with **`push: false`** first. That is a real dry run: it runs the
tests, computes the next version, and builds both the image and the package
without pushing any of them. Then run with `push: true`. The dry run is what
stops a successful tag from stranding on a failed build.

`image.yml` on its own, on a push to main, publishes `:main` and `:latest` for
testing; publish is the only workflow that ever writes a semver tag. Nothing
here deploys anything — that is the installer's job, against whatever image
tag it chooses.

## Things that already cost someone an afternoon

- **`USER` in the Dockerfile must be numeric.** A Kubernetes pod spec with
  `runAsNonRoot: true` refuses a non-numeric user — `image has non-numeric
  user (nobody), cannot verify user is non-root` — and sits in
  `CreateContainerConfigError`. It is `USER 65534` here; an installation's own
  `runAsUser` has to match it.
- **The image build hands a venv between stages, and it has two rules.**
  `/opt/venv` must be copied to the *same absolute path* it was created at — a
  venv records that path in `pyvenv.cfg` and in every console script's shebang,
  so landing it elsewhere points it at an interpreter that is not there. And
  `COPY . .` must stay *after* the dependency install: `.git` is in the build
  context for setuptools_scm, so copying the source first invalidates the
  dependency layer on every commit, docs-only ones included. Both are asserted
  in `tests/test_packaging.py`, because the shape they replaced — builder builds
  a wheel, runner installs it and every dependency a second time — reads as
  perfectly ordinary Dockerfile.
- **Do not reach for `ResourcesAsTools`, or back for `SkillsDirectoryProvider`.**
  Both enumerate every skill on every listing call — `resources/list` was 77KB
  for this catalogue before `Catalogue` replaced it with a dozen indexes.
  `SkillsDirectoryProvider` also keys a skill on its folder name alone, so two
  packs shipping a `testing/` collapse into one and the loser vanishes from the
  server entirely. Neither failure raises anything.
- **A provider that stores a `Catalogue` serves the old generation forever** —
  read through the getter (`lambda: school.snapshot.catalogue`), never a
  captured reference. A `Revalidator` has the same shape of bug and a nastier
  symptom: a refresh exports a source to a *new* directory, so one that outlived
  its snapshot goes on revalidating into a tree nothing is serving, and the edit
  never appears. It is built in `build_snapshot` and retired with the snapshot.
- **`live` is a per-file ETag revalidation, not fsspec's `filecache`.** The saga
  proposed chaining `filecache::webdav+https://…`, which is the off-the-shelf
  answer and the wrong one: it stores files under hashed names in a cache
  directory of its own, and everything downstream of a source here — the
  harvest, the URI grammar, the traversal guard, `_manifest` — needs a real
  directory tree of `Path`s. ~80 lines over webdav4 is the cost of keeping that
  invariant.
- **A fingerprint is published, so it stays small.** `build_snapshot` puts
  `record.fingerprint` straight into `/health` and `index.py` writes it to
  `index.json`. A WebDAV source's whole `{path: etag}` map went into both before
  it was reduced to a digest and a count. Whatever a new backend's fingerprint
  returns, it is a thing an operator reads and a thing rewritten to disk on
  every rebuild.
- **A live read blocks the whole event loop, and that is still true.** The
  resource and prompt handlers are `async`, and `Catalogue.read` under them is
  synchronous — so ten concurrent reads of a source 300ms away take 3.0s,
  perfectly serialized, and nothing else on the loop runs meanwhile, not
  another source's read and not `/health`. What is bounded is the *failure*:
  `REVALIDATE_TIMEOUT` caps one read and `COOLDOWN_SECONDS` stops a wedged
  server costing anything after the first. **Making the read path async — a
  thread for the blocking call, or an async webdav client — is the outstanding
  follow-up**, and it is a change to `resources.py`, `prompts.py` and
  `uris.py`, not to `live.py`.
- **An export is named by its version, so a rebuild of a version that has not
  moved is a rebuild over a tree being read.** That is the one case a repair
  happens at all — an export that lost files, or one a killed fetch left a temp
  file in. `Exports.ensure` publishes to the next free name (`<version>.1`)
  for exactly this reason; a "simplification" that renames onto `<version>`
  brings back 2% empty reads under load. `tests/test_exports.py` holds it.
- **wsgidav's ETag is `inode-mtime-size`.** The test server's, not Nextcloud's,
  which is content-derived — so a test that edits a served file must change its
  *length*, or two writes in the same second are one ETag and the revalidation
  test passes for the wrong reason.

## Where code goes

The split follows the layout proposed in `modelcontextprotocol/python-sdk#1681`:
**MCP wiring separate from tool implementations**, with pure logic factored out
of the handlers. FastMCP has no `APIRouter` equivalent (`PrefectHQ/fastmcp#948`
closed unanswered), so each module exposes a `register(mcp, index)` that the
server calls. Mounting sub-servers is the other option and is wrong here: it
namespaces tool names with a prefix, and these three names are the agent's API.

- **Adding a tool** → `tools.py`. Never `server.py`. But think first: the tools
  are a *mirror* of the resources, and a third tool that is not one breaks the
  promise that drives the whole design — that a client which knows how to read
  MCP resources already knows how to drive this server.
- **Changing the URI grammar, or what a URI resolves to** → `uris.py`. Both
  halves project from `Catalogue`, so a change there lands on both at once,
  which is the point.
- **Adding an endpoint** → `routes.py`.
- **Adding a prompt** → a file matching a source's `include.prompts` glob (or the
  `prompts/**/*.md` convention), named `<pack>_<file-stem>`. No code. Every
  placeholder must be a declared argument and a required argument cannot have a
  default; the server skips a file that breaks either rule, so
  `tests/test_prompts.py` loads a broken one to prove that.
- **A pack references files outside its skills** → add them to that source's
  `include.files`. They are served at `skill://<pack>/<path>` and listed under
  `skill://<pack>/_files`, never indexed as skills. The spec says a skill is
  self-contained, so most sources need none — grep the SKILL.md files for
  `shared/`-style paths before reaching for it.
- **Changing what counts as a skill, or who may see one** → `skills.py` for the
  rule, `uris.py` for where it is applied. Every `Catalogue` method takes
  `pinned`, so there is no method that can be called without deciding about the
  scope — a handler that reimplemented that filter is how a pinned client ends
  up seeing another pack.
- **A new flag or env var** → `main.py`, which is the whole configuration
  surface. Nothing else in the package reads `os.environ`, except the
  `{env: NAME}` resolver in `config.py`, which is the one other reader of the
  environment.
- **Anything that changes what the catalogue holds** goes through
  `School.refresh` and produces a new `Snapshot`; never mutate one.

`skills.py` imports no FastMCP, which is deliberate: the catalogue is testable
without an MCP client, and `tests/test_skills.py` exercises the scoping rules
directly rather than only through the tools.

## The surface, and why it is two tools

Resources are the interface. The tools are a mirror of them and nothing else:
`list_resources()` returns the rows `resources/list` returns, `read_resource(uri)`
takes the URI `resources/read` takes. An agent that can drive MCP resources can
drive this server without learning anything, which is the whole design.

Progressive disclosure moved into the address space, so adding packs adds
neither tools nor listing rows:

| call | returns | cost |
| --- | --- | --- |
| list | one index per pack and per group | ~1.9 KB for 90 skills |
| read `skill://grafana-lgtm` | that group's skills, as URIs | ~4k chars |
| read `skill://grafana/loki` | the instructions to follow | one file |

Reads return **only** what was asked for. A skill body may cite
`references/FOO.md`; citing it does not fetch it. That laziness is the point —
keep it when changing this code.

A client declares it cannot read resources with `?resources=off` or
`X-MCP-Resources: off`, and only then is the mirror listed. It stays callable
either way: hiding a tool from a listing is presentation, refusing to run one
would be a different and worse contract.

Two ways to hard-scope, both ceilings the model cannot widen past:

- **`X-Skill-Pack` header**, per client. One deployment serves many single-pack
  agents; in n8n it is a Header Auth credential on the MCP Client Tool node.
  Prefer this — a second copy of the server is a whole extra pod for no reason
  a header could not solve first.
- **A config that lists less**, per deployment. A deployment that should serve
  less gets a config that lists less — narrower blast radius but a whole pod.

They compose: the header narrows within whatever the config loads. Header
scoping only exists inside an HTTP request, so it is inert over stdio — the
tests in `tests/test_header_scope.py` run a real uvicorn server for that reason.

`GET /health` reports the libraries, the skill and prompt counts, the
catalogue's generation and when it was built, and each source's own status and
fingerprint:

```
{"status":"ok","generation":N,"built":"...","libraries":[...],"skills":N,"prompts":N,"sources":{...}}
```

`POST /reindex` returns the same payload plus `"rebuilt":[names]`. Between them
they answer the three questions worth asking of a running instance: did every
configured source load, which generation is being served, and has a refresh
picked up an edit yet.

## House rules that apply here

- Every PR needs a `CHANGELOG.md` entry under `[Unreleased]`; `pr.yml` enforces
  it. Dependabot PRs carry the `no changelog` label, and `pr.yml` also skips the
  gate for them unconditionally — a label is repository state anyone can delete,
  and Dependabot silently drops a label that does not exist.
- The gates on a PR are `Test (3.14)`, `Package`, and the four `quality.yml`
  jobs. The image is deliberately not built on a PR; `quality.yml` lints the
  Dockerfile and `package.yml` builds the wheel it wraps.
- `.github/zizmor.yml` and `.hadolint.yaml` record *why* each relaxed rule is
  relaxed. An ignore without a reason does not belong in either.
- No `Makefile`. Everything is `pyproject.toml` plus `scripts/requirements.py`.
- No auth on this server, by design: every skill it serves is public markdown
  that is already on GitHub. Do not add a token without a reason to.
