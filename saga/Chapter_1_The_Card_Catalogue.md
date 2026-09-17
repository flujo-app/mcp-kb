# Chapter 1 — The Card Catalogue

> Reading room, **SKILLS-MCP**, the front desk.
>
> Here is the library as it stands. We have a very good card catalogue. A
> reader walks up, asks what we hold, and gets a dozen drawer labels instead of
> ninety index cards. They pull a drawer, read the cards, and ask for exactly one
> book. Nobody carries the whole collection to the desk to answer "what is
> there?" — that lesson cost us 77 KB per listing and a five-second freeze, and
> it is written into the catalogue now.
>
> **And every book in the building was bound into the walls when the building
> was built.**
>
> To add a shelf we pour a new foundation. `skills.toml` names four collections,
> the image build fetches them, and the only way to change what is on the
> shelves is to publish a new building and move everybody into it.
>
> Meanwhile we learned what the catalogue actually is. It is not a list of
> skills. It is **one way to ask any source "what do you hold?" and "give me that
> one"** — and it does not care where the book physically lives. Claude Code
> worked that out first: every MCP server it talks to hides its reading room
> behind the same two verbs, list and read. A client that has those verbs does
> not need a tool per server. A client that lacks them has nothing.
>
> This chapter is about **interlibrary loan.** The building stops holding books.
> It holds a catalogue, and a card that says *this collection lives on GitHub,
> that one on the Nextcloud drive, that one is another library's reading room.*
> Change the cards, restart the desk, and the shelves are different.
>
> Chapter 1 is exposition and design. **We build nothing until the plan is
> signed off.** What follows is what exists, what we are adding, what we have
> decided, and the forks that are still Dr K's to close.

---

## Status: **OPEN — design, awaiting sign-off** — opened 2026-09-15

**Second pass, same day.** Dr K closed four forks and added three requirements
the first pass did not have: a name with `mcp` in it, a cache with an index, and
*libraries* plus *tags* as the organising idea. Part V carries those; the
sections they revise say so and are otherwise left as written, because the
reasoning is the point and a plan edited to match its own second draft teaches
nothing.

Nothing below is built. Decisions marked **(recommended)** are proposals; the
ones marked **(locked)** follow directly from something Dr K already said or
something the repo already enforces. Every **OPEN** fork is collected at the end
under *Closing questions*.

---

## Part I — Exposition: what the reading room holds today

A summary so a reader picking the repo up cold does not reconstruct it from
source. `AGENTS.md` carries the rules; this restates only what Part II builds on.

### The shape of the thing

`skills-mcp` is a FastMCP 4.0.3 server, deployed in the `flow` namespace, that
serves Agent Skills (`SKILL.md` folders) and a handful of prompts.

Five facts define everything else:

1. **One address space.** Everything is a `skill://` URI and reading one is the
   only operation. One segment is an index, two or more is content.
2. **The listing is indexes, not skills.** ~1.9 KB for ninety skills. A reader
   descends by reading index URIs whose bodies list child URIs. FastMCP has no
   `resources/directory/read`, so indexes-as-resources is the directory tree.
3. **Resources are the interface; tools are a mirror.** `list_resources` and
   `read_resource` return the same rows and bodies. They are hidden from any
   client that reads resources, and revealed by `?resources=off` or
   `X-MCP-Resources: off`.
4. **Scope is a ceiling.** `X-Skill-Pack` (per request) and `SKILL_PACKS` (per
   deployment) narrow every listing and every read, and an out-of-scope URI is
   indistinguishable from a missing one.
5. **Everything is baked at build time.** `skills.toml` pins four git sources by
   SHA; a Docker stage clones them into `/skills`; `prompts/` is copied to
   `/prompts`; a weekly workflow repins and opens a PR.

### The code, by responsibility

| Module | Owns | Survives this chapter? |
|---|---|---|
| `skills.py` | `Skill`, `load_skills` (walks for `SKILL.md`), `PackResources`, `SkillIndex` | yes — it already only sees a local directory |
| `uris.py` | `Catalogue`, the `skill://` grammar, manifests | yes, widened to more kinds |
| `resources.py` | `CatalogueProvider`, `HideMirrorTools` | yes |
| `tools.py` | the two mirror tools | **replaced** by FastMCP transforms (§C1.10) |
| `prompts.py` | prompt files, `{{ }}` rendering, `PromptProvider` | yes, gains dialects (§C1.8) |
| `request.py` | reads headers and query params | yes, renamed header (§C1.11) |
| `main.py` | argparse + env, the whole config surface | **reshaped** — gains `--config` |
| `scripts/fetch_skills.py`, `skills.toml`, `update-skills.yml`, the Dockerfile `skills` stage | build-time fetching | **deleted** (§C1.13) |

The load-bearing observation for Part II: **nothing downstream of `load_skills`
knows or cares where a directory came from.** Path, `rglob`, and a
resolve-then-`is_relative_to` traversal guard. That seam is where the whole
chapter plugs in.

---

## Part II — What we learned, and what already exists

### The ah-ha, stated precisely

Claude Code exposes `ListMcpResourcesTool` and `ReadMcpResourceTool` — two tools
that front the `resources/list` and `resources/read` of **every** connected
server, with a `server` argument. So a server with fifty readable things costs the
model zero extra tool definitions. Every other server with a `get_x` / `list_x`
tool pair is paying for something the protocol already gives away.

Two consequences shape this chapter:

- **For a client that has resources**, the best thing a server can do is put
  everything readable behind `resources/*` and advertise no tools at all. We
  already do this for skills.
- **For a client that lacks resources or prompts** (n8n's MCP Client Tool, many
  agent frameworks), this server can *be* those two verbs — for its own material
  and for other servers' material it proxies. That is the "ultimate answer" Dr K
  named, and it is a direct generalisation of the mirror we already have.

### Prior art, surveyed before inventing anything

#### The multi-backend filesystem: `fsspec`

**[fsspec](https://filesystem-spec.readthedocs.io/)** is the answer to "is there
a Python library that already handles multiple filesystem backends". It is the
storage layer under pandas, dask, xarray and Hugging Face.

- **2026.7.0, zero required dependencies, ~649M downloads/month.** Backends are
  optional extras or separate packages, so it costs nothing we do not use.
- **Built in:** `file`, `memory`, `git` (via pygit2), `github` (via requests),
  `http`, `zip`, `tar`, `sftp`, `smb`, `ftp`, `dir` (a path-prefix wrapper).
- **Registered but external:** `webdav`/`dav` → **webdav4**, `s3` → s3fs,
  `gs`/`gcs` → gcsfs, `gdrive` → gdrivefs/PyDrive2, `hf` → huggingface_hub,
  `sftp`/`ssh` → sshfs, `msgd` → Microsoft Graph.
- **URL chaining** (`simplecache::webdav://…`) and file caches are built in.

So Dr K's hope holds: pick fsspec as the seam and S3, Google Drive, SFTP and
Hugging Face become "install one package and write a URL". None of them is
required by this chapter.

#### WebDAV: `webdav4` over `webdavclient3`

| | **webdav4** | webdavclient3 |
|---|---|---|
| Latest | 0.11.0, 2026-02-19 | 3.14.7, 2026-02-06 (previous release 2021-08) |
| Downloads/month | ~560k | ~1.07M |
| Depends on | `httpx<1`, `python-dateutil` | `requests`, `lxml`, `python-dateutil` |
| fsspec | **is fsspec's registered `webdav://`** (`webdav4[fsspec]`) | none |
| Listing | PROPFIND `Depth: 1` per directory | — |

webdavclient3 has more downloads and no fsspec integration, and it brings `lxml`
(a compiled dependency). webdav4 is the implementation fsspec itself points at,
is actively released, and brings only `httpx`. **webdav4.** FastMCP 4.0.3
depends on `httpx2`, which installs as module `httpx2`, so adding webdav4's
`httpx` does not collide.

#### Git: `pygit2`, not fsspec's `github://`, not GitPython

| | **pygit2** | dulwich | GitPython | fsspec `github://` |
|---|---|---|---|---|
| Latest | 1.20.1 | 1.2.15 | 3.1.62 | (built in) |
| Depends on | `cffi` (libgit2 bundled in the wheel) | `urllib3` | `gitdb` **plus a `git` binary** | `requests` |
| Wheels | cp314 manylinux x86_64 **and aarch64** | pure Python | pure Python | — |
| Shallow clone | `clone_repository(depth=…, bare=…)` | `porcelain.clone(depth=…)` | via CLI | n/a |
| fsspec | **`git://` is built on it** | none | none | is fsspec |
| Downloads/month | ~4.7M | — | — | — |

- **fsspec's `github://` is the wrong tool** despite being the obvious one. It
  talks to the REST API, one request per directory, and GitHub allows 60
  unauthenticated API requests an hour. Grafana's pack alone is dozens of
  directories. It would work in a test and rate-limit in the second restart.
- **GitPython shells out to `git`**, which the runner image deliberately does not
  carry.
- **pygit2** is the backend fsspec's own `git://` uses — `GitFileSystem` opens a
  repository and reads trees at a ref, with no working tree — and its wheels
  bundle libgit2, so the runner still has no `git` binary. One protocol, `git`,
  covers GitHub, GitLab, Gitea and anything else with an HTTPS clone URL, which
  is what Dr K asked for: *not GitHub-specific, but GitHub gets special focus.*
- dulwich is the credible alternative (pure Python) and is the fallback if
  pygit2's shallow fetch of a pinned SHA misbehaves against GitHub — see
  *Verify before building* in §C1.6.

#### Frontmatter: `python-frontmatter`

`skills.py._frontmatter` and `prompts.py._split` are two hand-rolled copies of
the same parser with slightly different failure behaviour. **python-frontmatter**
1.3.0 depends only on `pyyaml` (already present), ~11.8M downloads/month.
Replacing both is the kind of tech debt Dr K asked this research to remove.

#### Config: pydantic, which is already here

`fastmcp-slim` already depends on `pydantic` and `pydantic-settings`. A pydantic
model gives validation, precise error messages and a **publishable JSON Schema**
(`model_json_schema()`) for free, so the config file gets editor completion
through the `# yaml-language-server: $schema=` modeline this cluster already uses
everywhere. pydantic-settings' YAML source is not needed: the config file says
*what to serve*, env and flags keep saying *how to run*, and those stay separate.

#### MCP proxying: FastMCP already has it

- **`ProxyProvider(client_factory, cache_ttl=300)`** exists in 4.0.3 and proxies
  an upstream server's tools, resources, templates and prompts through our own.
- **`mcp.add_provider(provider, namespace="x")`** rewrites resource URIs
  `proto://path` → `proto://x/path` and prompt names → `x_name`, and reverses
  them on the way back. That is the collision story, already written.
- **Transforms** exist for the client-capability gap: `ResourcesAsTools`
  (`list_resources`, `read_resource`) and `PromptsAsTools` (`list_prompts`,
  `get_prompt`). Both route through the server, so provider scoping still applies.
- A provider has a `lifespan()` hook, which is where a source that must be cloned
  or copied before serving does its work.

#### The GitHub conventions worth translating

Where material lives in repos today, collected from the VS Code, Copilot, Claude
Code and Agent Skills docs:

| Kind | Conventional paths |
|---|---|
| Skills | `skills/**/SKILL.md`, `.github/skills/*/SKILL.md`, `.claude/skills/*/SKILL.md`, `.agents/skills/*/SKILL.md` |
| Prompts | `.github/prompts/*.prompt.md` (VS Code/Copilot), `commands/*.md` (Claude Code plugins), `prompts/**/*.md` (ours) |
| Instructions | `.github/copilot-instructions.md`, `.github/instructions/*.instructions.md` (`applyTo:` glob), `AGENTS.md`, `CLAUDE.md` |
| Agents | `.github/agents/*.agent.md`, `agents/*.md` (Claude Code plugins) |

Prompt files come in three dialects, which matters for §C1.8:

| Dialect | Arguments | Placeholder |
|---|---|---|
| ours | `arguments:` list in frontmatter | `{{ name }}` |
| VS Code `.prompt.md` | none declared; `argument-hint:` for the input box | `${input:name}` / `${input:name:placeholder}` |
| Claude Code `commands/*.md` | `argument-hint:` | `$ARGUMENTS`, `$1`, `$2` |

---

## Part III — The doctrine

### §C1.1 — Decision (recommended): the server serves *readable material from sources*; skills are one kind of it

The job is no longer "serve skills". It is:

> **Read material from configured sources — files, git, WebDAV, other MCP
> servers — and serve it through MCP's reading primitives: resources and
> prompts. For clients that lack those primitives, be them.**

Skills stay the flagship kind and keep their grammar. Prompts, instructions and
agent files are kinds alongside them. Tools are never a kind: this server does
not execute anything, and a proxied server's tools are deliberately dropped
(§C1.9).

That scope is also why a rename is on the table — see §C1.14.

### §C1.2 — Decision (recommended, REVISED — see §C1.18): every filesystem source becomes a local directory before anything reads it

This is the central decision and the one everything else leans on.

There are two ways to serve from a remote filesystem:

| | **A. Snapshot at startup** | B. Serve live through fsspec |
|---|---|---|
| What the catalogue sees | a local directory | an fsspec filesystem |
| Changes to `skills.py` / `uris.py` | none | rewrite every `Path`, `rglob`, `resolve` |
| Traversal guard | the existing resolve-then-compare, unchanged | reimplemented per backend |
| A read when Nextcloud is down | served | fails |
| Read latency | disk | a network round trip per read |
| An edit in Nextcloud shows up | on restart | on read, **but not in listings**, which are still built at startup |
| Matches "a restart is all I need" | exactly | goes beyond it, halfway |

**A**, for three reasons:

1. **The seam already exists.** Part I's observation: nothing past
   `load_skills` knows where a directory came from. A snapshot plugs in with
   zero changes to the catalogue, the grammar, the scoping or the traversal
   guard, which are the four things this repo has already paid for in bugs.
2. **B is half a feature.** Listings must be built once at startup — the #8
   lesson is that walking a pack per request freezes the event loop, and over
   WebDAV every directory is a PROPFIND. So under B a new Nextcloud file still
   needs a restart to appear; only *edits* would be live. That is a
   surprising, hard-to-explain half.
3. **The protocol is uniform.** Every fsspec backend supports
   `fs.get(root, dest, recursive=True)`. So a handler is: *build an fsspec
   filesystem from the source, copy its root into the cache directory.* Git is
   one extra step first (clone), and `file://` skips the copy and serves in place.

So the handler contract is one method:

```python
class Mirror(Protocol):
    def materialise(self, cache: Path) -> Path:
        """Return a local directory holding this source's files."""
```

and a source that is an **MCP server** is not a filesystem at all. It is a
provider, served live, and it gets its own family (§C1.9). Two handler families,
not four special cases:

| Family | Schemes | Produces |
|---|---|---|
| **Mirror** | `file`, `git`, `webdav` (+ any fsspec protocol later) | a local directory |
| **Proxy** | `mcp` | a FastMCP `Provider` |

**Hot reload is carried forward, not designed away.** MCP has
`notifications/resources/list_changed` and FastMCP sends it when components
change. A later chapter can re-materialise on a timer and announce the change.
Snapshot-at-startup is the foundation for that, not an obstacle to it.

### §C1.3 — Decision (recommended): one config file, mounted, validated, with a published schema

Dr K's shape: *a configuration file you mount to the running server like any
other config; it has resource links to skills and prompts; a restart is all I
need.*

- **`CONFIG` env / `--config` flag**, default `/etc/skills-mcp/config.yaml`.
  In the cluster it is a ConfigMap; kustomize's `configMapGenerator` hashes it
  into the name, so **editing the config rolls the pod** — the restart happens by
  itself.
- **The file says what to serve. Env and flags say how to run.** `TRANSPORT`,
  `HOST`, `PORT` stay where they are. No setting lives in both places.
- **Validated at startup, strictly.** An unknown key is an error, not ignored —
  a typo'd `inculde:` that silently serves nothing is the worst failure a config
  file has. A *source* that fails to load at runtime is a different matter
  (§C1.12).
- **The schema is published.** `skills-mcp schema` prints the JSON Schema from
  the pydantic model; it is committed as `config.schema.json`, and a test fails
  if the committed file drifts from the model. Every example config starts with
  the `yaml-language-server` modeline, so editing one in VS Code is completion
  and red squiggles.

### §C1.4 — Decision (recommended, REVISED — see §C1.17 and §C1.19): the schema

#### The whole file

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/kubed-io/skills-mcp/main/config.schema.json
sources:
- name: superpowers
  url: git+https://github.com/obra/superpowers.git
  ref: b36e0829c6d0140e93cfef2ca599b1b07d4a7797

- name: grafana
  url: github://grafana/skills
  ref: 51d33e71e191b409bbd25fc7be2684c610d18166

- name: penpot
  url: github://penpot/penpot-ai-kit
  ref: c63d8e3717323fad859e794848e5a602b155a7ec
  include:
    files:
    - shared/**
    - workflows/**

- name: kubed
  url: file:///srv/prompts

- name: drive
  url: webdav+https://drive.example.com/remote.php/dav/files/drk/Agents
  auth:
    username:
      env: NEXTCLOUD_USER
    password:
      env: NEXTCLOUD_PASSWORD

- name: n8n
  url: mcp+http://n8n-mcp.flow.svc.cluster.local:8000/mcp
  auth:
    headers:
      Authorization:
        env: N8N_MCP_TOKEN
  tools:
  - uri: n8n://workflows/{id}
    tool: get_workflow_details
    description: One n8n workflow, as its full JSON.
```

#### The rules, and why each is shaped the way it is

**`sources` is a list, and `name` is the identity.** A name becomes the pack in
`skill://<name>/…`, the prefix in `<name>_<prompt>`, and the namespace for a
proxied server. It is validated as `^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$` —
the Agent Skills spec's name rule, so a source name is always a legal skill-ish
identifier — and must be unique. **Rejected, never slugged**, on selenium-flow's
§F1.4 reasoning: a rewritten name is a pack nobody can find.

**The URL scheme is the discriminator.** This is the protocol-handler idea made
structural. Each scheme maps to one pydantic model, assembled into a
discriminated union with a callable discriminator on the scheme, so an error on a
`webdav+https` source says what a WebDAV source accepts rather than listing every
field of every type:

| Scheme | Model | Family |
|---|---|---|
| `file://` | `FileSource` | mirror, served in place |
| `git+https://`, `git+http://`, `github://` | `GitSource` | mirror |
| `webdav+https://`, `webdav+http://` | `WebdavSource` | mirror |
| `mcp+https://`, `mcp+http://` | `McpSource` | proxy |

**`git+https`, not `git://` — deliberately different from how Dr K wrote it.**
`git://` is already a real thing: git's own unauthenticated protocol on port
9418, and the MCP spec lists it as a resource URI scheme for version control. A
config that said `git://github.com/…` and meant HTTPS would be a lie a reader
has to know to see through. `git+https://` is pip's VCS URL spelling, so it is
also already familiar. The same `<handler>+<transport>` rule then gives
`webdav+https` and `mcp+https` for free, and one rule is easier than three
exceptions. **`github://org/repo`** is the one shorthand, because GitHub is where
most public skills live; it expands to `git+https://github.com/org/repo.git`.

**Structural fields, not a packed string.** pip packs the ref and subdirectory
into the URL (`…@ref#subdirectory=skills`). Selenium-flow's §F1.7 settled that
this fleet prefers a field to a mini-language inside a string, and it applies
here: `ref` is a field, validated on its own, and reported on its own in
`/health`.

**Secrets are references, never values.** `{env: NAME}` is the only way to supply
a credential, modelled on Kubernetes' `valueFrom`: the config names *where* a
secret is, and the file stays safe to commit and to publish as a ConfigMap. A
plain string where a secret is expected is refused at startup. The resolved value
is a pydantic `SecretStr`, so it cannot land in a log or a `/health` body by
accident. `env` is the only source in this chapter; `file:` (a mounted Secret
key) is the obvious second and costs one line when it is wanted.

**`include` is globs per kind, with conventions as the default.** Leaving
`include` out means "use the conventions" (Part II's table), which covers every
source in `skills.toml` today *and* `.github/` repos with no configuration:

```yaml
include:
  skills:       [skills/**/SKILL.md, .github/skills/*/SKILL.md, .claude/skills/*/SKILL.md, .agents/skills/*/SKILL.md]
  prompts:      [prompts/**/*.md, .github/prompts/*.prompt.md, commands/*.md]
  instructions: [.github/copilot-instructions.md, .github/instructions/*.instructions.md, AGENTS.md, CLAUDE.md]
  agents:       [.github/agents/*.agent.md, agents/*.md]
  files:        []
```

Setting a kind **replaces** its default rather than appending to it — an append
would leave no way to *stop* serving a convention. An empty list turns a kind
off. This replaces `skills.toml`'s `path` and `extras`:

- `path = "skills"` existed to keep grafana's root `template/SKILL.md` out. The
  default glob `skills/**/SKILL.md` never matches the root, so the gotcha
  disappears rather than being documented.
- `extras = ["shared", "workflows"]` becomes `files: [shared/**, workflows/**]`.

**A `file://` source whose directory holds skill folders directly** — the local
development case, `file:///tmp/skills/superpowers` — needs `include: {skills:
["*/SKILL.md"]}`. That is one line, and it is better than a default so loose it
matches templates.

**What each family accepts**, beyond `name`, `url` and `include`:

| Field | file | git | webdav | mcp |
|---|---|---|---|---|
| `ref` | — | commit SHA, tag or branch; default: the remote's default branch | — | — |
| `auth.username` / `auth.password` | — | optional (private repos; a GitHub token is the password) | yes | — |
| `auth.headers` | — | — | — | `{Header: {env: …}}` |
| `include` | yes | yes | yes | **no** — see `serve` |
| `serve` | — | — | — | `[resources, prompts]` by default; either can be dropped |
| `tools` | — | — | — | tool → resource mappings (§C1.9) |

Where a field is not accepted the model forbids it, so `ref` on a WebDAV source
is an error at startup, not a no-op.

### §C1.5 — Decision (recommended): `file://`

The simplest handler and the one E1 ships with. It serves the directory in place
— no copy — so a developer's edit shows up on restart and a mounted ConfigMap or
PVC works exactly as mounted. It replaces `SKILLS_DIR` and `PROMPTS_DIR`: those
become two `file://` sources, and the two env vars and their flags are deleted,
not kept as aliases. No compatibility shims is house policy, and the deployment
that sets them is ours.

### §C1.6 — Decision (recommended): `git` — clone once, read the tree at the ref, never check out

The handler, in order:

1. Clone into `<cache>/git/<name>` with pygit2, **bare** (no working tree to
   write) and **shallow** (`depth=1`).
2. If `ref` is a SHA not reachable from the shallow tip, fetch that SHA
   directly at depth 1. GitHub permits fetching any reachable commit by SHA.
3. Copy the tree at `ref` into `<cache>/src/<name>` through fsspec's
   `GitFileSystem(path=<bare clone>, ref=ref)` and `get(recursive=True)`.
4. Record the resolved commit SHA; `/health` reports it per source.

`ref` is optional, and that is a policy change worth stating. `skills.toml`
insisted on a SHA because the image had to be reproducible. Now the *config* is
the thing that is versioned: a SHA pins, a branch floats and a restart
re-fetches. Dr K's own repos will want a branch, and upstream packs will want a
SHA. `/health` showing the resolved SHA is what keeps a floating ref debuggable.

**GitHub's special focus**, concretely:

- the `github://org/repo` shorthand;
- the `.github/` conventions in the default `include`, so pointing at any repo
  that follows Copilot's layout serves its skills, prompts, instructions and
  agents with no configuration;
- the VS Code prompt dialect (§C1.8), so `.github/prompts/*.prompt.md` become
  real MCP prompts — slash commands in Claude Code;
- private repos through `auth.password: {env: GITHUB_TOKEN}` with username
  `x-access-token`.

None of it is GitHub-*only*: a Gitea repo with a `.github/` folder gets the same
treatment, because the conventions are about the tree, not the host.

**Verify before building, in the pod, not by reasoning:** (a) pygit2 1.20.1's
shallow clone plus a depth-1 fetch of an old pinned SHA against GitHub, since
libgit2's shallow support is recent; (b) that `GitFileSystem` opens a **bare**
repository — its constructor takes any path `pygit2.Repository` accepts, which
should include one, but "should" is what the pod is for. If (a) fails, dulwich
replaces step 1 and nothing else changes.

### §C1.7 — Decision (recommended): `webdav` — webdav4, credentials from env, copied at startup

- `webdav+https://host/remote.php/dav/files/<user>/<folder>` → webdav4's
  `WebdavFileSystem(base_url="https://host/remote.php/dav/files/<user>", auth=…)`
  and the folder as its root. The `+https` is stripped, and the rest of the URL is
  exactly the WebDAV address Nextcloud shows in its settings.
- Auth is `username`/`password` from env — Dr K's "simple username/password env
  var pair". For Nextcloud that should be an **app password**, not the account
  password, which the README will say.
- Materialised with `get(recursive=True)` into `<cache>/src/<name>`. Every
  directory is one PROPFIND, which is precisely why this happens once at startup
  and never per request.

**What it gives Dr K:** write `Agents/prompts/review-pr.md` in Nextcloud, restart
the pod, and `drive_review-pr` is in the slash menu. The memory
*slow MCP servers miss slash commands* is the constraint on the other side: the
copy must finish before the server answers `prompts/list`, or the first client to
connect after a restart sees an empty menu. §C1.12 decides that.

### §C1.8 — Decision (recommended): prompts speak three dialects, and instructions and agents are resources

**Prompts.** A prompt file's dialect is detected from its path, not guessed from
its body:

| Path | Dialect | Becomes |
|---|---|---|
| `*.prompt.md` | VS Code | each distinct `${input:x}` / `${input:x:hint}` → an optional argument `x`, described by `hint`; `argument-hint` → the prompt's description when it has none |
| `commands/*.md` | Claude Code | `$ARGUMENTS` → one optional argument `arguments`; `$1`…`$9` → `arg1`…`arg9` |
| any other `.md` | ours | unchanged: declared `arguments`, `{{ name }}` |

Unknown frontmatter — VS Code's `agent`, `model`, `tools` — is **kept in the
prompt's `meta`, not applied**. An MCP prompt is text a person picks; it cannot
switch the client's model or tools, and pretending otherwise would be the
"prompts can declare agent properties" misconception this project started from.
Keeping it in `meta` means a client that *can* use it still sees it.

All three dialects render with the same substitution code; only the placeholder
pattern and the argument extraction differ. No templating engine is added:
Jinja would make every `{%` in a LogQL body a syntax error.

**Instructions and agents are resources, not prompts.** Neither is something a
person fills in. An instructions file is context and an agent file is a
definition, and both are material an agent *reads*:

```
instructions://<source>/<name>     e.g. instructions://kubed/python
agent://<source>/<name>            e.g. agent://drive/reviewer
```

Each kind gets one index row in the listing, exactly as skills do, so adding a
`.github/instructions/` folder with forty files adds one row, not forty. An
instructions resource's description carries its `applyTo` glob, because that is
the part a reader needs to decide whether it applies.

**An agent resource is a definition, not a subagent.** Claude Code cannot load a
subagent from MCP; agent definitions only come from `.claude/agents/`, the user
directory, managed settings or plugins. Serving them is still useful —
readable, and syncable to disk by a client that wants to — but the README must
not imply more.

### §C1.9 — Decision (recommended): `mcp` — resources and prompts only, namespaced, plus tool → resource mappings

*Resource namespacing superseded by §C1.23 (decision 7): a proxied server's resource URIs pass through verbatim. Prompt namespacing and the tool → resource mappings stand.*

**The proxy.** A `ProxyProvider` subclass whose `_list_tools` returns nothing and
whose tool lookup finds nothing, added with `namespace=<name>`. So a proxied
server's resource `n8n://workflow-sdk/reference` is served as
`n8n://n8n/workflow-sdk/reference`, and its prompt `debug` as `n8n_debug`.
Resource templates come through too. Tools never do: this server does not
execute anything, and proxying tools would turn a read-only catalogue into a
remote-execution gateway with a different security story entirely.

**Why this is worth having**, in Dr K's words: *some MCP servers serve resources
and prompts that are not accessible at all if the client does not support those
features.* n8n's own MCP server publishes its workflow SDK reference as a
resource. An n8n agent cannot read it. Proxied here, and mirrored as tools
(§C1.10), it can.

**The bonus: a tool mapped to a resource.** Plenty of servers put readable things
behind `get_x(id)` tools. A mapping declares one as a resource template:

```yaml
tools:
- uri: grafana://dashboards/{uid}
  tool: get_dashboard_by_uid
  description: A Grafana dashboard's JSON model, by UID.
```

- `uri` is an RFC 6570 template and **its variables are the tool's arguments**,
  by name. A variable with no matching tool argument is an error at startup,
  checked against the upstream tool's input schema. That is selenium-flow's
  "validate the reference at save time" rule, applied at load time.
- Reading `grafana://dashboards/abc` calls `get_dashboard_by_uid(uid="abc")` and
  returns the tool's text content as the resource body.
- Only arguments that appear in the URI can be passed. A tool needing more is
  not a resource, and forcing it to be one is how a read ends up with side
  effects.
- Mapped URIs are **not** namespaced — they are authored here, not upstream, so
  the author already chose them. A collision with another source is a startup
  error.

This is how a server with no resources at all gets redirected "through what
should have been" resources.

**What we cannot know from here and must verify in the pod:** that
`add_provider(namespace=…)` rewrites *template* URIs as well as static ones; that
upstream auth headers ride on `ProxyClient`'s transport as expected; and what the
`cache_ttl` default (300 s) means for a proxied `resources/list` that the
upstream changes. The first live target is the n8n MCP server, because its
resource is the motivating case.

### §C1.10 — Decision (recommended): one "list" and one "read" for every client that lacks them — and the same for prompts

Today a client declares `?resources=off` and gets `list_resources` /
`read_resource`, hand-written in `tools.py` over our own catalogue. Two things
change:

1. **The mirror must cover proxied material too**, so it has to go through the
   server rather than the catalogue. FastMCP's `ResourcesAsTools` does exactly
   that.
2. **Prompts need the same treatment**: `?prompts=off` / `X-MCP-Prompts: off`
   reveals `list_prompts` and `get_prompt` from `PromptsAsTools`.

**`ResourcesAsTools` was rejected before, and the reason no longer holds.**
`AGENTS.md` says never to reach for it because it enumerates every resource on
every call. That was true when `resources/list` was 77 KB. The listing is now
indexes, so the transform returns exactly what `resources/list` returns — which
is the whole promise of a mirror. That reversal must be written into `AGENTS.md`
with the reason, or the next reader will undo it by following the old rule.

`HideMirrorTools` stays and grows a second switch: resource mirrors hidden unless
`resources=off`, prompt mirrors hidden unless `prompts=off`. A client with
neither capability sees four tools. A spec-complete client sees none.

**Verify before building:** that `ResourcesAsTools`' listing includes templates
in a shape a model can use (`uri_template`), and that both transforms go through
`CatalogueProvider`'s scoping for a pinned client. If either fails, `tools.py`
is widened instead and the transform is not adopted.

### §C1.11 — Decision (recommended, SUPERSEDED by §C1.19): scope by source, and the header says so

A pack was a source all along. `X-Skill-Pack` becomes **`X-Source`**, still a
ceiling, now applied to every kind and every proxied server. `SKILL_PACKS` is
deleted: a deployment that should serve less gets a config that lists less, which
is what a config file is for.

A group pin (`grafana-lgtm`) keeps working for skills and widens to that source
for the other kinds, as prompts already do today.

**A spec note, so it is a decision and not an accident.** The 2026-07-28 spec
says `resources/list` "MUST NOT vary per-connection" but "MAY vary by the
authorization presented on the request". A per-request header set once in a
client's credential, which is how n8n sets it, is on the permitted side of that
line. A query parameter someone pastes is closer to the other side, which is
another reason the header wins over the parameter when both are present.

### §C1.12 — Decision (recommended): a broken source is skipped, reported, and never blocks the others

- **Startup materialises every source before the server accepts connections.**
  So the first `prompts/list` after a restart is complete, which is what keeps
  prompts in the slash menu. Sources materialise concurrently.
- **Each source has a timeout** (`SOURCE_TIMEOUT`, default 60 s). A source that
  fails or times out is logged and skipped. The server starts with the rest.
  This is the same rule a malformed prompt file already follows: one bad input
  must not take down everything else.
- **`/health` reports per source:** `ok`/`failed`, the resolved `ref` for git,
  counts per kind, and the error text for a failure — with credentials never in
  it, which `SecretStr` enforces rather than hopes.
- **Readiness stays green with a failed source.** A failed Nextcloud should not
  take the GitHub packs offline too. `/health` stays useful for alerting on
  `failed` without the kubelet killing a pod that is mostly working.
- **The cache is disposable.** `<cache>` (`CACHE_DIR`, default
  `/var/cache/skills-mcp`) is an `emptyDir` in the cluster, which also satisfies
  `readOnlyRootFilesystem: true`. It is rebuilt on every start, so there is no
  stale-cache bug to have.

### §C1.13 — Decision (recommended): the image stops holding books

Deleted, in the epic that makes them unnecessary:

- the Dockerfile's `skills` stage and `COPY prompts /prompts`;
- `scripts/fetch_skills.py`, `skills.toml` and `tests` that read it;
- `.github/workflows/update-skills.yml`.

**Who bumps pinned SHAs now is an open question (#5).** The weekly repin PR was
genuinely useful: a bump is new upstream *instruction* content and deserves a
read. With the pins living in the cluster repo's ConfigMap, the options are
Renovate (a regex manager can bump a SHA in YAML; Dependabot cannot), a small
workflow in the cluster repo, or floating on branches for upstreams Dr K trusts.

`prompts/grafana/debug-logs.md` moves too, and where it moves is part of the same
question: either a `file://` source mounted from a ConfigMap in the cluster repo,
or a `git+https` source pointing back at this repo.

### §C1.14 — **CLOSED in §C1.16**: the name

The project no longer serves only skills and no longer holds anything itself.
Candidates, checked 2026-09-15: `kubed-librarian`, `kubed-shelf` and
`kubed-lectern` are all free on PyPI, and `kubed-io/librarian` and
`kubed-io/shelf` are free on GitHub:

| Name | Says | Against |
|---|---|---|
| **`librarian`** (recommended) | one desk that finds material wherever it lives, including other libraries' — interlibrary loan is the MCP proxy | common word; `librarian-mcp` is taken on PyPI, though `kubed-librarian` is not |
| `shelf` | short; a shelf holds what you read | says nothing about fetching or proxying |
| `lectern` | a lectern is where you read *and* speak from: resources and prompts | too clever by half |
| keep `skills-mcp` | no churn; the listing is still mostly skills | the name would be wrong about half of what it serves |

A rename touches the repo, the Python package (`kubed.skills_mcp` →
`kubed.<name>`), the console script, the image, the Service name every client
URL uses, the `flow` namespace deployment, the `/mcp__skills__*` slash command
prefix in every client, and the saga folder itself. **That is cheap now and
expensive after E3**, so if it happens it is E0.

### §C1.15 — Decision (locked): each epic is one PR in one session

Standing rule. No stacking, and no second PR in another repo from the same
session — the cluster-repo rollout in E8 is its own session.

---

## Part IV — The plan

### Global constraints

- Python `>=3.10`; CI sweeps 3.10–3.14; the image runs 3.14.
- FastMCP `>=4.0.0,<5`. New runtime dependencies are exactly:
  `fsspec`, `pygit2`, `webdav4[fsspec]`, `python-frontmatter`. Nothing else.
- Kustomize patches are JSON6902 only. No strategic merge patches.
- No `Makefile`. `pyproject.toml` plus scripts.
- Every PR has a `CHANGELOG.md` line under `[Unreleased]`; breaking changes say
  **BREAKING:**.
- Nothing outside `main.py` reads `os.environ` — except the `{env:}` secret
  resolver, which is the config layer's documented exception and lives in
  `config.py`.
- No psalm-style heavy tooling in the pod: `ruff` and `pytest` only, installed
  with `pip install --target` per the pod's Python notes.
- `.feature` files are never edited as part of the work.
- Real names never appear in this repo: Dr K, `drive.example.com`, `drk`.

### File map after Chapter 1

| Path | Responsibility | Epic |
|---|---|---|
| `kubed/skills_mcp/config.py` | pydantic models, scheme discriminator, `{env:}` secrets, `load_config` | E1 |
| `kubed/skills_mcp/sources/__init__.py` | `Mirror` protocol, scheme → handler registry, `materialise_all` | E1 |
| `kubed/skills_mcp/sources/file.py` | `file://` | E1 |
| `kubed/skills_mcp/sources/git.py` | `git+https`, `github://` | E2 |
| `kubed/skills_mcp/sources/webdav.py` | `webdav+https` | E3 |
| `kubed/skills_mcp/sources/mcp.py` | resources+prompts proxy, tool → resource templates | E5, E6 |
| `kubed/skills_mcp/harvest.py` | applies `include` globs to a materialised directory; yields skills, prompts, instructions, agents, files | E1 (skills, prompts, files), E4 (the rest) |
| `kubed/skills_mcp/dialects.py` | the three prompt dialects | E4 |
| `kubed/skills_mcp/skills.py` | unchanged API; frontmatter via python-frontmatter | E1 |
| `kubed/skills_mcp/uris.py` | + `instructions://`, `agent://` | E4 |
| `kubed/skills_mcp/tools.py` | **deleted**; replaced by transforms | E7 |
| `config.schema.json` | generated, committed, drift-tested | E1 |
| `examples/config.yaml` | the §C1.4 file, trimmed to what exists at each epic | E1 onward |

### The epics — **superseded by §C1.20**

| Epic | Delivers | Depends on | Breaking |
|---|---|---|---|
| **E0** | the rename, if §C1.14 closes on one | — | yes |
| **E1** | config file, schema, `file://`, harvest globs for skills/prompts/files, python-frontmatter; `SKILLS_DIR`/`PROMPTS_DIR`/`SKILL_PACKS` removed; the image still bakes skills and ships a config pointing at them | — | yes |
| **E2** | `git+https` and `github://`; the image stops baking; `fetch_skills.py`, `skills.toml`, `update-skills.yml` deleted; `/health` per source; `emptyDir` cache in `deploy/` | E1 | yes |
| **E3** | `webdav+https` | E1 | no |
| **E4** | prompt dialects; `instructions://` and `agent://`; `.github` conventions complete | E1 | no |
| **E5** | `mcp+https` proxy of resources and prompts | E1 | no |
| **E6** | tool → resource mappings | E5 | no |
| **E7** | `ResourcesAsTools` + `PromptsAsTools`, `?prompts=off`, `X-Source`; `tools.py` deleted | E1 | yes (header) |
| **E8** | cluster repo: ConfigMap + Secret + `emptyDir`, the Nextcloud source live, the n8n MCP proxied | E2, E3, E5 | — |

E3, E4, E5 and E7 are independent of each other once E1 lands, so their order is
Dr K's preference. The default order above follows what he named first: dynamic
config, then Nextcloud, then proxying, then the tool-only answer.

Each epic after E1 gets its bite-sized task plan written at the start of its own
session, against the code as it then stands. A step-by-step plan for E5 written
today would be describing a `config.py` that does not exist yet, and would be
wrong in the details that matter.

### E1 — config file, schema, and `file://`: the task plan

**Goal:** the server's catalogue comes from a validated config file of sources,
and `file://` is the first handler. Behaviour is otherwise unchanged.

**Architecture:** `config.py` parses and validates; `sources/` turns each source
into a local directory; `harvest.py` applies `include` globs to that directory;
the existing `load_skills` / `PackResources` / `load_prompts` consume what
harvest yields, per source, with the source name as the pack.

#### Task 1: the config model and the `{env:}` secret

**Files:** create `kubed/skills_mcp/config.py`, `tests/test_config.py`.

**Produces:** `load_config(path: Path) -> Config`; `Config.sources: list[Source]`;
`FileSource(name: str, url: str, include: Include)`; `Include` with
`skills | prompts | instructions | agents | files: list[str] | None`;
`EnvRef(env: str)`; `ConfigError(ValueError)`.

- [ ] **Step 1: write the failing tests**

```python
from pathlib import Path

import pytest

from kubed.skills_mcp.config import ConfigError, FileSource, load_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_a_file_source_loads_with_conventional_includes(tmp_path):
    config = load_config(write(tmp_path, "sources:\n- name: kubed\n  url: file:///srv/prompts\n"))
    [source] = config.sources
    assert isinstance(source, FileSource)
    assert source.include.skills is None  # None means "use the conventions"


def test_an_unknown_key_is_refused_not_ignored(tmp_path):
    with pytest.raises(ConfigError, match="inculde"):
        load_config(write(tmp_path, "sources:\n- name: a\n  url: file:///a\n  inculde: {}\n"))


def test_an_unknown_scheme_names_the_schemes_that_exist(tmp_path):
    with pytest.raises(ConfigError, match="file://"):
        load_config(write(tmp_path, "sources:\n- name: a\n  url: ftp://a\n"))


@pytest.mark.parametrize("name", ["Bad", "-a", "a-", "a" * 65, "a/b", ".."])
def test_a_bad_source_name_is_rejected_not_slugged(tmp_path, name):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f"sources:\n- name: '{name}'\n  url: file:///a\n"))


def test_duplicate_source_names_are_refused(tmp_path):
    text = "sources:\n- name: a\n  url: file:///a\n- name: a\n  url: file:///b\n"
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write(tmp_path, text))
```

- [ ] **Step 2: run them and watch them fail**

Run: `pytest tests/test_config.py -q`
Expected: collection error, `ModuleNotFoundError: kubed.skills_mcp.config`.

- [ ] **Step 3: write the model**

```python
"""The config file: which sources to serve, validated before anything is read."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

NAME = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"


class ConfigError(ValueError):
    """The config file is unreadable or invalid."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvRef(Strict):
    env: str

    def resolve(self) -> SecretStr:
        # The one read of os.environ outside main.py: a secret is resolved where
        # it is declared, so it never passes through a plain str on the way.
        if self.env not in os.environ:
            raise ConfigError(f"environment variable {self.env} is not set")
        return SecretStr(os.environ[self.env])


class Include(Strict):
    skills: list[str] | None = None
    prompts: list[str] | None = None
    instructions: list[str] | None = None
    agents: list[str] | None = None
    files: list[str] | None = None


class FileSource(Strict):
    name: str = Field(pattern=NAME)
    url: str
    include: Include = Include()

    @field_validator("url")
    @classmethod
    def _file_url(cls, url: str) -> str:
        if not urlsplit(url).path.startswith("/"):
            raise ValueError("a file:// URL must be absolute: file:///path")
        return url

    @property
    def path(self) -> Path:
        return Path(urlsplit(self.url).path)


SCHEMES = {"file": "file"}

# One member for now. E2 turns this into
# Annotated[Annotated[FileSource, Tag("file")] | Annotated[GitSource, Tag("git")],
#           Discriminator(<scheme of url>)]
# — a discriminator needs a union of at least two, so it arrives with the second.
Source = FileSource


class Config(Strict):
    sources: list[Source] = Field(default_factory=list)

    @field_validator("sources", mode="before")
    @classmethod
    def _known_scheme(cls, raw: object) -> object:
        for item in raw if isinstance(raw, list) else []:
            scheme = urlsplit(str(item.get("url", ""))).scheme if isinstance(item, dict) else ""
            if scheme not in SCHEMES:
                known = ", ".join(f"{s}://" for s in SCHEMES)
                raise ValueError(f"unknown source scheme {scheme!r}; source schemes: {known}")
        return raw

    @field_validator("sources")
    @classmethod
    def _unique(cls, sources: list[Source]) -> list[Source]:
        seen: set[str] = set()
        for source in sources:
            if source.name in seen:
                raise ValueError(f"duplicate source name: {source.name}")
            seen.add(source.name)
        return sources


def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Config.model_validate(raw)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not YAML: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
```

When E2 adds `GitSource`, `SCHEMES` gains `"git+https": "git"` and friends, and
`Source` becomes the discriminated union in the comment. The before-validator
stays: it is what makes an unknown scheme say which schemes would have worked,
where pydantic's own "unable to extract tag" would not.

- [ ] **Step 4: run the tests and watch them pass**

Run: `pytest tests/test_config.py -q`
Expected: `10 passed`.

- [ ] **Step 5: break it on purpose.** Change `extra="forbid"` to `"ignore"`, run
  the tests, and confirm `test_an_unknown_key_is_refused_not_ignored` fails.
  Revert. A test that cannot fail is not a test.

- [ ] **Step 6: commit** — `feat(config): validated config file of sources`

#### Task 2: the published schema, and its drift test

**Files:** modify `kubed/skills_mcp/main.py`; create `config.schema.json`,
`tests/test_schema.py`.

**Consumes:** `Config` from Task 1. **Produces:** `skills-mcp schema` subcommand.

- [ ] **Step 1: write the failing test**

```python
import json
from pathlib import Path

from kubed.skills_mcp.config import Config

ROOT = Path(__file__).resolve().parents[1]


def test_the_committed_schema_is_the_model():
    committed = json.loads((ROOT / "config.schema.json").read_text())
    assert committed == Config.model_json_schema(), (
        "config.schema.json is stale: run `skills-mcp schema > config.schema.json`"
    )
```

- [ ] **Step 2: run it** — expected FAIL, file not found.
- [ ] **Step 3: add the subcommand** to `build_parser`: a `schema` positional
  choice that prints `json.dumps(Config.model_json_schema(), indent=2)` and
  exits before any server is built. Generate the file with it.
- [ ] **Step 4: run it** — expected PASS. Add a field to `Include` without
  regenerating and confirm it fails; revert.
- [ ] **Step 5: commit** — `feat(config): publish the config JSON Schema`

#### Task 3: `file://` and the harvest globs

**Files:** create `kubed/skills_mcp/sources/__init__.py`,
`kubed/skills_mcp/sources/file.py`, `kubed/skills_mcp/harvest.py`,
`tests/test_harvest.py`.

**Consumes:** `FileSource`, `Include`. **Produces:**
`materialise(source: Source, cache: Path) -> Path`;
`DEFAULTS: dict[str, tuple[str, ...]]`;
`skill_dirs(root: Path, include: Include) -> list[Path]`;
`prompt_files(root: Path, include: Include) -> list[Path]`;
`pack_files(root: Path, include: Include) -> list[str]`.

- [ ] **Step 1: write the failing tests** — build a tree in `tmp_path` with
  `skills/grafana-lgtm/loki/SKILL.md`, a root `template/SKILL.md`,
  `.github/prompts/review.prompt.md`, `prompts/grafana/debug-logs.md` and
  `shared/tokens.md`, then assert:

```python
def test_conventions_find_nested_skills_and_never_a_root_template(tree):
    found = {p.relative_to(tree).as_posix() for p in skill_dirs(tree, Include())}
    assert found == {"skills/grafana-lgtm/loki"}


def test_setting_a_kind_replaces_its_defaults(tree):
    found = skill_dirs(tree, Include(skills=["template/SKILL.md"]))
    assert [p.name for p in found] == ["template"]


def test_an_empty_list_turns_a_kind_off(tree):
    assert prompt_files(tree, Include(prompts=[])) == []


def test_files_are_served_only_when_asked_for(tree):
    assert pack_files(tree, Include()) == []
    assert pack_files(tree, Include(files=["shared/**"])) == ["shared/tokens.md"]


def test_a_glob_cannot_escape_the_source(tree):
    assert pack_files(tree, Include(files=["../**"])) == []


def test_a_file_source_is_served_in_place(tree):
    source = FileSource(name="kubed", url=f"file://{tree}")
    assert materialise(source, tree / "cache") == tree
```

- [ ] **Step 2: run them** — expected FAIL, import errors.
- [ ] **Step 3: implement.** `harvest.py` resolves each match and keeps it only
  if `is_relative_to(root.resolve())` — the same guard `uris.py` uses, applied
  once at harvest. Dotfile directories are skipped *except* the conventional
  `.github`, `.claude` and `.agents` prefixes named in `DEFAULTS`, which is the
  one place that exception lives. `sources/file.py` returns `source.path` after
  checking it is a directory, raising `SourceError` otherwise.
- [ ] **Step 4: run them** — expected PASS. Delete the `is_relative_to` check
  and confirm `test_a_glob_cannot_escape_the_source` fails; revert.
- [ ] **Step 5: commit** — `feat(sources): file:// sources and include globs`

#### Task 4: wire the catalogue to sources, and delete the old configuration

**Files:** modify `skills.py`, `prompts.py`, `server.py`, `main.py`,
`tests/conftest.py`, every test constructing `SkillsMCP`; create
`examples/config.yaml`; modify `Dockerfile`, `deploy/deployment.yaml`,
`kustomization.yaml`, `README.md`, `AGENTS.md`, `CHANGELOG.md`.

**Consumes:** Tasks 1–3. **Produces:** `SkillsMCP(config: Config, cache: Path)`.

- [ ] **Step 1: change the fixture first.** `conftest.py` writes a config with
  one `file://` source per test pack instead of setting `SKILLS_DIR`. Run the
  whole suite and watch the construction sites fail.
- [ ] **Step 2: replace both frontmatter parsers** with `frontmatter.loads`,
  keeping each call site's existing tolerance: `skills.py` treats a malformed
  block as empty; `prompts.py` treats it as an error. Run
  `tests/test_skills.py tests/test_prompts.py` — expected PASS, unchanged.
- [ ] **Step 3: build the catalogue per source.** `load_skills` takes the skill
  directories harvest found and the source name as the pack, rather than walking
  a base directory for packs; the group rule (the directory containing a skill)
  is unchanged. `PackResources` takes `pack_files`. `load_prompts` takes
  `prompt_files`, pack = source name.
- [ ] **Step 4: delete `SKILLS_DIR`, `PROMPTS_DIR`, `SKILL_PACKS`** and their
  flags; add `--config` / `CONFIG` and `--cache-dir` / `CACHE_DIR`. No aliases.
- [ ] **Step 5: ship a config in the image.** The Dockerfile still bakes
  `/skills` this epic; it copies `examples/config.yaml`, which lists one
  `file:///skills/<pack>` source per pack with `include: {skills: [...]}`
  matching the old `path` and penpot's `files`. So the deployed catalogue is
  byte-for-byte what it was.
- [ ] **Step 6: prove it in the pod, not only in tests.** Build, port-forward,
  and diff `resources/list` and a read of `skill://penpot/_files` against the
  running v-previous. Identical output is the acceptance test for E1.
- [ ] **Step 7: document.** `AGENTS.md` "Adding a skill pack" becomes "Adding a
  source"; the `SKILL_PACKS` scoping paragraph becomes "a config that lists
  less"; README configuration table rewritten. CHANGELOG:
  `**BREAKING:** configuration is a config file of sources; SKILLS_DIR, PROMPTS_DIR and SKILL_PACKS are gone.`
- [ ] **Step 8: commit** — `feat!: serve from a config file of sources`

### Self-review of this plan

- **Spec coverage.** Config + schema → §C1.3–4, E1. `file` → §C1.5, E1. `git` +
  GitHub focus → §C1.6, E2, E4. `webdav` → §C1.7, E3. `mcp` resources/prompts
  → §C1.9, E5. Tool → resource → §C1.9, E6. Universal list/read for tool-only
  clients, prompts included → §C1.10, E7. Rename → §C1.14, E0. Package research
  → Part II. Nothing Dr K named is unassigned.
- **Placeholders.** E2–E8 are deliberately task-level, and the reason is stated
  above rather than left as a TBD.
- **Names.** `materialise`, `Include`, `FileSource`, `load_config`,
  `ConfigError`, `SourceError` and `DEFAULTS` are spelled the same in every task
  that uses them.

---

## Part V — Second pass: the name, the cache, and libraries

### §C1.16 — Decision (locked by Dr K, 2026-09-15): the name is `mcp-school`

*Superseded by §C1.22 — the name is `mcp-kb`. The reasoning below stands; only
the word it landed on changed.*

Dr K wants `mcp` in the name and something that reads as a knowledge dump, and
proposed two: **`mcp-school`** first, then **`mcp-resource-gateway`**. Both are
free on PyPI, npm and `kubed-io` as of 2026-09-15, along with `mcp-library`,
`mcp-archive`, `mcp-academy`, `mcp-stacks`, `mcp-lectern`, `mcp-reading-room`
and `mcp-desk`. (`mcp-canon` is taken on PyPI, `mcp-bookshelf` on npm, and bare
`mcp-gateway` on both.)

**`mcp-resource-gateway` is the accurate one, and it has a problem worth knowing
before choosing it.** "MCP gateway" is a crowded, well-defined product category
in 2026 — Microsoft's Kubernetes reverse proxy for MCP servers, IBM's
ContextForge, Unla, TrueFoundry, and an `awesome-mcp-gateways` list to hold them
all. Every one of them means *the control point in front of many MCP servers
that aggregates their **tools**, authenticates callers and routes calls.*

This server deliberately does the opposite of the defining half of that: §C1.9
drops proxied tools on the floor, on purpose, because a read-only catalogue that
starts executing other servers' tools is a different product with a different
security story. So the name would promise the one thing we refuse to do, put us
in a search results page owned by nine other projects, and still only describe
half of what we serve — prompts are a first-class kind and are not resources.
It is also long enough to be felt at every call site, where the client prefix
becomes `mcp__mcp-resource-gateway__*`.

**Dr K closed this on `mcp-school`**, agreeing that gateway is a crowded term.
It is short, it says "knowledge" the way he asked, it collides with no category,
and it earns the metaphor the rest of the chapter runs on: a school has a
library, its material is organised into subjects, and it is somewhere you go to
be *taught* rather than to be executed against.

The general rule, worth keeping for the next thing this fleet names: **a name
that lands inside an existing product category inherits that category's
promises.** `gateway` promised tool proxying, which is the one thing §C1.9
refuses.

**What is worth keeping from `mcp-resource-gateway` either way** is the phrase
itself, as the repo description and the README's first line: *a resource and
prompt gateway for MCP.* That is the clearest one-sentence statement of §C1.1
anyone has written so far, and it belongs in the project whether or not it is
the name.

The rename lands as **E0**, first, before any of this is built:

| What | From | To |
|---|---|---|
| repo | `kubed-io/skills-mcp` | `kubed-io/mcp-school` |
| distribution | `kubed-skills-mcp` | `mcp-school` |
| package | `kubed.skills_mcp` | `mcp_school` |
| console script | `skills-mcp` | `mcp-school` |
| image | `kubed/skills-mcp` | `kubed/mcp-school` |
| Service, Deployment | `skills-mcp` | `mcp-school` (namespace `flow` unchanged) |
| the client prefix | `mcp__skillsmcp__*` | `mcp__school__*` |

The distribution name loses the `kubed-` prefix because `mcp-school` is free on
PyPI outright, and `kubed-skills-mcp` only ever carried the prefix to avoid a
collision that does not exist here. Everything below keeps saying "this server"
rather than either name, so none of it rots on the rename.

### §C1.17 — Decision (recommended): the URL form follows PEP 610, which is the standard that already exists

Dr K asked the right question — *we cannot be the only ones, there has to be an
off-the-shelf idea here.* There is, and four ecosystems have converged on nearly
the same answer.

| Ecosystem | Spelling |
|---|---|
| **pip / PEP 508** | `git+https://host/repo.git@<ref>#subdirectory=<path>` |
| **npm** | `git+https://host/repo.git#<committish>` (or `#semver:^1.2`) |
| **SPDX 2.3** `PackageDownloadLocation` | `git+https://git.myproject.org/MyProject.git@v1.0` |
| **Terraform** | `git::https://example.com/vpc.git//<subdir>?ref=v1.2.0&depth=1` |

Three of the four spell the transport exactly as `git+https://`, which settles
§C1.4's deviation from Dr K's `git://`: the `+` form is not our invention, it is
the majority spelling, and it exists precisely *because* `git://` already means
git's own protocol. Terraform is the outlier with `git::` and query parameters,
and it is also the only one that thought about shallow clones — its `depth=1`
is the same optimisation §C1.6 makes by default.

**The real find is [PEP 610](https://packaging.python.org/en/latest/specifications/direct-url-data-structure/)**,
the Direct URL Data Structure, which is a published standard for *a VCS location
as structured data rather than a packed string.* Its shape:

```json
{"url": "https://github.com/obra/superpowers.git",
 "vcs_info": {"vcs": "git", "requested_revision": "main",
              "commit_id": "b36e0829c6d0140e93cfef2ca599b1b07d4a7797"},
 "subdirectory": "skills"}
```

Three things fall straight out of it, and all three are better than what §C1.4
invented:

1. **`ref` as a field is standard, not a house style.** PEP 610 calls it
   `requested_revision` and defines it as "a tag name, branch name, Git ref,
   commit hash, shortened commit hash, or other commit-ish" — exactly the
   `ref/sha/branch/tag` Dr K asked for in his answer 4.
2. **Requested and resolved are two different fields.** `requested_revision` is
   what the config asked for; `commit_id` "MUST be a commit hash ... in order to
   reference an immutable version of the source code". That is precisely the
   distinction §C1.6 needed for a floating branch, and it is what the index
   records as a source's fingerprint (§C1.18). We adopt the names: `ref` in the
   config, `commit` in the index.
3. **Credentials in a URL may be environment variables.** PEP 610 permits
   `${VAR}` in the user:password section specifically so a persisted URL carries
   no secret. Our `{env: NAME}` says the same thing in YAML, so §C1.4's secret
   rule is a spelling of an existing standard rather than a local invention.

So the config **is** PEP 610's data structure with friendlier key names, and
`config.schema.json` can say so. Nothing changes in §C1.4's shape; it is now
argued from a standard rather than from taste, and `subdirectory` is added as an
optional field for the one case globs handle clumsily — a repo whose skills live
under a path that also appears elsewhere in the tree.

### §C1.18 — Decision (recommended): an index file is the catalogue; the payload is cached beside it

This replaces §C1.2's "snapshot everything" with something with a dial on it,
because Dr K wants two things that pull apart: **a cold start fast enough that
prompts appear in the slash menu**, and **a file edited in Nextcloud usable
immediately.**

#### The split that makes both possible

> **The index is always local. The payload is cached or fetched, per source.**

An index entry is small — a URI, a name, a description, tags, a library, a path
and a hash. Ninety skills of it is a few hundred KB of JSON. A payload is the
file itself, and only a read needs one.

`<cache>/index.json`, written at startup, read at the next one:

```jsonc
{
  "version": 1,
  "built": "2026-09-15T21:40:11Z",
  "sources": {
    "grafana-skills": {
      "status": "ok",
      "cache": "snapshot",
      "fingerprint": {"commit": "51d33e71e191b409bbd25fc7be2684c610d18166"},
      "root": "src/grafana-skills",
      "components": [
        {"kind": "skill", "uri": "skill://grafana/loki", "name": "loki",
         "library": "grafana", "tags": ["grafana", "upstream", "skill"],
         "description": "Query and debug Loki…",
         "path": "src/grafana-skills/skills/grafana-lgtm/loki/SKILL.md",
         "sha256": "…"}
      ]
    }
  }
}
```

This is the shape every neighbouring ecosystem landed on independently: npm's
`package-lock.json` beside `node_modules/`, `npx skills`' `skills-lock.json`
beside `.agents/skills/`, Skilldex's `skilldex.json` manifest recording each
skill's source URL and install time, and registry repos that ship an `index.json`
next to the payload. A manifest that is cheap to read, separate from the material
it describes.

#### The cold start, which is the whole point

The memory that drives this: Claude Code builds its slash-command list from the
servers that have **answered** by the time it looks, and a five-second
`resources/list` is why this server's one prompt never appeared.

So the startup order is not negotiable:

1. **Read `index.json`. Serve.** No network, no walk. The server is answering
   `initialize`, `prompts/list` and `resources/list` in milliseconds.
2. **Then, in the background**, check each source's fingerprint and re-harvest
   only what changed.
3. **When a source changes, announce it** — `notifications/resources/list_changed`
   and `notifications/prompts/list_changed`, which FastMCP sends when components
   change. A client that is already connected picks the new material up without
   reconnecting.

Only the very first boot with an empty cache has no index to serve, and even
then sources materialise concurrently with a per-source timeout (§C1.12).

**Fingerprints are one cheap call per source:**

| Source | Fingerprint | Cost to check |
|---|---|---|
| `git` | the resolved commit | one remote ref lookup; a pinned SHA needs no call at all |
| `webdav` | every child's ETag and mtime | **one PROPFIND per directory** — verified: webdav4 passes `etag` straight through into fsspec's `info`, so a `Depth: 1` listing prices a whole folder in one request |
| `file` | mtime and size | a walk, local |

#### The dial: `cache: snapshot | live`

Per source, defaulting to `snapshot`:

| | `snapshot` (default) | `live` |
|---|---|---|
| Payload | copied into `<cache>/src/<name>` at index time | fetched on read |
| A read | disk | revalidated, then disk or network |
| Edit a file at the source | visible after the next reindex | **visible on the next read** |
| Source is down | reads still work | reads fail |
| Suits | git, anything pinned | the Nextcloud folder Dr K edits |

`live` is not hand-rolled: fsspec ships `filecache` with `check_files=True` and
`expiry_time`, which is a read-through cache that revalidates against the
backend and re-downloads only changed bytes. Chained onto the source URL
(`filecache::webdav+https://…`), a read of an unchanged file costs one
conditional request and no transfer. That is the off-the-shelf part Dr K asked
for, and it is why `live` is a config value rather than a second code path.

*Built otherwise — see E4. `filecache` stores files under hashed names in a
directory of its own, where everything downstream here needs a real tree of
`Path`s, so `live` is a per-file ETag revalidation written over webdav4; and a
source that is down degrades to the cached copy rather than failing the read.*

**What `live` does *not* buy is a new file appearing.** A file that did not exist
at index time has no URI, so it cannot be read by name. That is what `refresh`
is for:

```yaml
- name: drive-prompts
  url: webdav+https://drive.example.com/remote.php/dav/files/drk/Prompts
  cache: live
  refresh: 5m      # re-fingerprint in the background; announce what changed
```

`refresh` unset means "only at startup", which is right for a pinned git source
and wrong for the drive. Dr K's answer 4 — *updating should be a simple matter of
reindexing or restarting the pod* — is then literally true: a restart rebuilds,
and `refresh` does it without one.

**Two more things fall out for free:**

- **`POST /reindex`** rebuilds on demand, for when five minutes is too long to
  wait. It is the same code the timer runs.
- **`cache_ttl` on the server** (FastMCP 4.0.3, SEP-2549) tells *clients* how
  long they may reuse a listing. Set it to the shortest `refresh` in the config,
  with `cache_scope: private` because listings vary by the caller's scope
  (§C1.19). A client that opts in stops asking at all between refreshes.

### §C1.19 — Decision (recommended): a library is the grouping, tags are the cross-cut, and both are selectable

*The URI shape `skill://<library>/<skill>` below is superseded by §C1.23. The library/tag model stands — and its retirement of `pack` and `X-Skill-Pack`, recorded here but never done in code, is decided again there and scheduled in §C1.24.*

This supersedes §C1.11, and it is Dr K's idea: *something like "library" to cover
one little group of skills + prompts + mcp-tool-to-resource, so that something
like "grafana" can attach a skill and some prompts and they appear to be in the
same grouping.*

**The first pass conflated two things.** A "pack" was both *where material came
from* and *what it is about*. Those are different questions the moment Grafana
material arrives from three places: skills from GitHub, prompts from the drive,
and mapped resources from the Grafana MCP server.

So:

- **A source is provenance.** Where bytes come from, how they are fetched, what
  credentials that needs.
- **A library is subject.** What the material is *about*. It is the unit a
  reader browses and the unit a client scopes to.
- **A source declares which library it joins**, defaulting to its own name. So
  the simple case is unchanged and the interesting case is one line:

```yaml
libraries:
- name: grafana
  description: Grafana, Loki and the rest of the LGTM stack.
  tags: [observability]

sources:
- {name: grafana-skills, library: grafana, url: "github://grafana/skills", ref: 51d33e7…}
- {name: drive-grafana,  library: grafana, url: "webdav+https://…/Prompts/grafana", cache: live}
- {name: grafana-mcp,    library: grafana, url: "mcp+http://grafana-mcp.monitoring:8000/mcp"}
```

Reading `skill://grafana` now lists **everything in the library across kinds** —
the upstream skills, Dr K's prompts, and the dashboard resource mapped off the
MCP server's tool — which is exactly the "they appear to be in the same
grouping" he asked for. `pack` disappears from the vocabulary; the URI is
`skill://<library>/<skill>`, and it looks identical for every source that did
not ask to join one.

**Tags are the cross-cut.** Every component is registered with tags, which
FastMCP components already carry and expose in `_meta`:

| Tag | Where it comes from |
|---|---|
| the library name | always |
| the source name | always |
| the kind (`skill`, `prompt`, `instructions`, `agent`) | always |
| anything in `tags:` on the library or the source | config |
| a skill's own frontmatter tags | upstream, if present |

So `observability`, `upstream` and `experimental` cross libraries, while the
library stays the tidy grouping.

**A client selects with either, and both are ceilings:**

```
?library=grafana,n8n      X-MCP-Library: grafana,n8n
?tags=observability       X-MCP-Tags: observability
```

Allowlists, intersected, applied to every listing and every read, with an
out-of-scope URI indistinguishable from a missing one — the rule §C1.11 already
enforces, widened from one pack to two dimensions. `X-Skill-Pack` and
`SKILL_PACKS` are both deleted.

**These are filtered in our own provider, not with FastMCP's `Visibility`
transform, and that is a finding rather than a preference.** FastMCP's per-session
visibility rules are stored in *session state* (`save_visibility_rules` writes to
`context`), which assumes a session that persists across requests. This server's
clients are largely stateless HTTP, where each request may be its own session,
and the per-request filter in `CatalogueProvider` already works there and is
tested. `Visibility` stays useful for what it is for — a server-level allowlist —
and is not the mechanism for per-caller scope here.

**The memoised listing.** Dr K: *the combo of params can be like a memoized cache
of resources so using it is real quick.* Exactly:

```python
key = (frozenset(libraries), frozenset(tags), full)
```

A dict from that key to the finished list of listing rows, built on the first
request that asks for it and reused afterwards. It is **cleared whenever the
index is rebuilt**, which is the only time it can go stale, and that is one line
in the reindex path rather than a cache-invalidation problem. Realistically a
deployment sees a handful of distinct keys — one per connected client — so the
dict stays small; a cap with least-recently-used eviction is one line if it ever
does not.

The combination is the performance story end to end: **a listing is a dict
lookup over an index that was read from disk**, and nothing in the request path
touches a network or a filesystem walk.

### §C1.20 — The revised epics

| Epic | Delivers | Depends on |
|---|---|---|
| **E0** | the rename to `mcp-school` (§C1.16) | — |
| **E1** | config file, JSON Schema, `file://`, harvest globs, libraries and tags in the model, python-frontmatter | E0 |
| **E2** | the index, `index.json`, cold start from it, background refresh, `list_changed`, memoised listings, `POST /reindex` | E1 |
| **E3** | `git+https` and `github://`; the image stops baking; `fetch_skills.py`, `skills.toml` and `update-skills.yml` deleted | E2 |
| **E4** | `webdav+https`, `cache: live` via fsspec `filecache`, `refresh` | E2 |
| **E5** | prompt dialects; `instructions://` and `agent://` | E1 |
| **E6** | `mcp+http` proxy of resources and prompts | E1 |
| **E7** | tool → resource mappings | E6 |
| **E8** | `ResourcesAsTools` + `PromptsAsTools`, `?prompts=off`, the `library`/`tags` selection params | E2 |
| **E9** | cluster rollout: ConfigMap, Secret, `emptyDir` cache, the drive source live, the n8n MCP proxied | E3, E4, E6 |

**What moved and why.** The index (E2) comes before any remote backend, because
it is the seam every backend writes into — fingerprint, components, payload path.
Adding it after three backends would mean retrofitting three. It is also the
epic that pays Dr K's stated priority: after E2 the server starts instantly and
the slash-command problem is structurally gone, before there is any network
source to make it slow again.

E1's task plan in Part IV stands, with two additions to Task 1: `library: str |
None` and `tags: list[str]` on a source, and an optional top-level `libraries:`
block. The Task 1 code and its ten tests were run against pydantic and pass; the
two new fields are one line each and do not change its shape.

---

### §C1.21 — An idea, not built: prompts as `prompt://` resources

Weighed while designing the prompt mirror (2026-09-16), and set aside on purpose.

The appeal was real. Listing prompts as resources — `prompt://grafana` an index,
`prompt://grafana/debug-logs` the template with its argument schema, and
`?app=nextcloud` on the URI to render it — would put prompts in the one discovery
surface every resource-reading client already has. A model using Claude Code's
resource tools would find them with no new tools at all. `library` and `tags` would
filter them through the same code path as skills, and there would be two mirror
tools rather than four.

It was set aside because the protocol keeps prompts and resources apart for a
reason. A prompt is role-tagged messages, possibly several — it can seed a
conversation, assistant turns included — where a resource read is content. Folding
one into the other flattens exactly what makes it a prompt. So the mirror is
FastMCP's own `PromptsAsTools`, which keeps that shape, and prompts stay a separate
kind. Worth revisiting only if a client appears that reads resources but will never
call a prompt tool.

---

### §C1.22 — Decision (locked by Dr K, 2026-09-16): the name is `mcp-kb`

`school` was the answer to §C1.16's question — a word that says "knowledge",
collides with no product category, and earns the metaphor the chapter runs on.
It is still all three. What it is not is *literal*: a reader meeting
`mcp-school` for the first time has to be told what it holds, where **kb** tells
them. Knowledge base is the category this actually is, nobody has to be taught
the abbreviation, and it is two letters at every call site where the metaphor
was costing seven.

The general rule of §C1.16 survives intact, and is worth restating because it is
the one that rejected `mcp-resource-gateway`: **a name that lands inside an
existing product category inherits that category's promises.** "Knowledge base"
promises a collection you read. That is exactly the product.

| What | From | To |
|---|---|---|
| repo | `kubed-io/mcp-school` | `kubed-io/mcp-kb` |
| distribution | `mcp-school` | `kubed-mcp-kb` |
| package | `mcp_school` | `kubed.mcp_kb` |
| console script | `mcp-school` | `mcp-kb` |
| image | `kubed/mcp-school` | `kubed/mcp-kb` |
| paths | `/etc/mcp-school`, `/var/cache/mcp-school` | `/etc/mcp-kb`, `/var/cache/mcp-kb` |
| the catalogue class | `School` | `KnowledgeBase` |

The `skill://` scheme, the `X-Skill-*` headers and the `CONFIG` / `CACHE_DIR`
environment variables are deliberately unchanged: they are the contract a
deployed client holds, and none of them says the project's name.

**The distribution regains the `kubed-` prefix, and the package moves into the
`kubed` namespace.** `kubed.mcp_kb` sits beside `kubed.selenium_flow` in a
PEP 420 namespace package — which means **there is no `kubed/__init__.py` in
either repository**, and a test in each asserts it. Add one and the two stop
installing side by side, silently, in whichever environment resolves second.

The one-sentence statement of §C1.1 that §C1.16 wanted for the repository
description travels with the rename, minus the word it was wrong about: *an MCP
knowledge base — skills, prompts and agent material collected from git, WebDAV
and folders into one catalogue, served as resources, or as tools for clients
without them.*

**The package is organised by responsibility, on the sibling's shape.**
`catalogue/` is what is served and how it is found, addressed and persisted;
`sources/` is where bytes come from, with revalidation among them because
`cache: live` is a source concern and not a serving one; `mcp/` is what an agent
sees, and the per-request scope that shapes it; `spec/` is the OpenAPI document
for the HTTP surface. `server.py` keeps `KnowledgeBase` and stays the
composition root. The seams were already in the module boundaries — this only
made the directories agree with them.

**The wiki is the manual.** `README.md` advertises and links; `AGENTS.md` holds
the rules and the traps; the wiki at `kubed-io/mcp-kb/wiki` — a submodule at
`wiki/`, three of its pages generated from the live config models, a live
server's tool list and the OpenAPI document — is where depth goes. A generated
page cannot describe a server that no longer exists, and `tests/test_wiki.py`
fails when a committed page and the generator disagree.

**The first release computes from a `v0.0.1` tag on the first commit.** Nothing
has ever shipped under either name, so the CHANGELOG's first section is `Added`
and nothing else: there is no version for a `Changed` or a `Fixed` to be
relative to. `duplocloud/version-bump` needs a base tag to bump from and 404s on
an untagged repository *after* a green dry run, so the tag goes on before the
first publish rather than being discovered by it.

### §C1.23 — Decision (locked by Dr K, 2026-09-16): skill URIs follow the MCP Skills extension, and the library is the first segment

**How this came up.** Loading the grafana pack and reading its URIs side by side
with what installing the same repository as a Claude Code plugin produces showed
three things wrong with the address space §C1.19 left behind, none of which the
tests could see because the tests asserted the shape they were written against:

| On disk | Installed as a Claude Code plugin | Served by mcp-kb (0.0.x) |
|---|---|---|
| `skills/grafana-lgtm/loki/SKILL.md` | plugin `grafana-lgtm`, skill `grafana-lgtm:loki` | `skill://grafana/loki` |
| `skills/grafana-k6/k6/SETUP.md` | a file in plugin `grafana-k6` | `skill://grafana/k6/SETUP.md` |
| the folder `skills/grafana-lgtm/` | a plugin you choose to install | `skill://grafana-lgtm`, an index at the top level |

1. **A folder looked like a skill.** `skill://grafana-lgtm` sat beside
   `skill://grafana` at the top of the address space and returned a list of
   skills, which reads as a skill named `grafana-lgtm` with sub-skills in it. It is
   a directory — the upstream packages it as a *plugin*, and "group" was this
   project's own word for it, not the ecosystem's.
2. **The instructions lived at a directory address.** `skill://grafana/loki`
   returned `SKILL.md`. On a standard FastMCP skill server that address is not
   found, so an agent that learned the shorthand here fails against selenium-flow
   and the reverse.
3. **`pack` never left.** §C1.19 retired the word and deleted `X-Skill-Pack`; the
   config and the scoping moved to `library`, but the code, the served
   descriptions ("The grafana pack — 50 skills"), the URI grammar in the docs and
   the header alias all kept `pack`. The decision was recorded and not carried out.

**There is a standard, and it answers every question this raised.** The MCP Skills
extension — SEP-2640, extension identifier `io.modelcontextprotocol/skills`, a
released specification at `modelcontextprotocol/ext-skills`
(`specification/stable/skills.mdx`) — defines how skills are served over MCP.
What it settles:

- **Grammar.** `skill://<skill-path>/<file-path>`. `<skill-path>` is one or more
  segments, "nested to arbitrary depth". Its **final segment MUST equal the skill's
  `name`**; the preceding segments are "a server-chosen organizational prefix …
  by domain, team, version, or any other axis". `skill://acme/billing/refunds/SKILL.md`
  is the spec's own example: prefix `acme/billing`, skill `refunds`.
- **The skill's URI names `SKILL.md`.** A skill is `skill://<skill-path>/SKILL.md`;
  the bare `skill://<skill-path>` is defined as the skill's *root directory*.
- **Identity is the URI, not the name.** "A skill's `name` is a label, not an
  identifier." Within a server the URI identifies a skill; across servers it is
  the pair of server identity and URI. Two skills named `refunds` at different
  paths are both legitimate.
- **A prefix is not a skill.** Nesting means a `SKILL.md` inside another skill's
  directory, and it carries its own consent rules. A directory with no `SKILL.md`
  is organisation, nothing more.
- **Loading by URI is required.** "Hosts MUST support loading by URI, including
  skills that do not appear in a listing", and an unknown URI is `-32602`. A URI a
  server names in its instructions, an error message or another skill has to
  resolve exactly as written.
- **Relative references resolve against the skill's root**, "as a filesystem path
  would resolve".
- **Discovery gets its own methods, content does not.** A server declaring the
  extension implements `skills/list` (flat, paginated, each entry carrying the
  frontmatter and a manifest of every file with its SHA-256 and size) and
  `skills/get`. Files are still read with `resources/read`: a skill is still
  resources. `resources/directory/read` is optional behind `directoryRead: true`.

Adoption, as the extension's own implementation table records it on 2026-09-16:
the four official SDKs have it in progress; FastMCP's `SkillsProvider` is
`pre-v1` with "its own `skill://` shape" (which is where `_manifest` comes from);
Hugging Face's server is `v1`; fast-agent, the MCP Inspector, MCPJam and ChatGPT
plugins are `partial` hosts. Claude Code's own client in this session exposes
resource tools only — list, read, and a directory read that answers "Directory
listing is not enabled in this build".

**What this chapter decides.**

**1. A skill's URI is `skill://<library>/<path>/<name>/SKILL.md`.** `<path>` is the
skill directory's path below its source's conventional skill root — the roots
`harvest.SKILL_ROOTS` already knows (`skills/`, `.github/skills/`,
`.claude/skills/`, `.agents/skills/`, or the source root itself) — so it mirrors
the disk and invents nothing.

| On disk (source root →) | URI |
|---|---|
| `skills/grafana-lgtm/loki/SKILL.md` | `skill://grafana/grafana-lgtm/loki/SKILL.md` |
| `skills/grafana-k6/k6/SETUP.md` | `skill://grafana/grafana-k6/k6/SETUP.md` |
| `skills/brainstorming/SKILL.md` (superpowers) | `skill://superpowers/brainstorming/SKILL.md` |

Stripping the conventional root keeps the URI short without changing any
relationship between skills: every skill in a source loses the same leading
segment, so a sibling reference such as superpowers' `../using-superpowers/…`
or grafana's `../testing/SKILL.md` still resolves to the skill it names. That is
the property the spec's relative-resolution rule depends on, and 0.0.x broke it
for grafana by dropping the plugin folder.

**2. The library is the first segment, and it does the job "server" does across
servers.** A host keys a skill by server plus URI; inside one mcp-kb that
aggregates many libraries, the library prefix is what keeps two libraries'
skills apart. A `library=` argument beside the URI was considered and rejected:
`resources/read` takes a URI and nothing else, so a native resource reader could
never pass it, and the tool mirror would stop being a mirror (§C1.10). The
connection scope `?library=grafana` remains the way to make one client *see* one
library — it narrows what is served, it does not rename it.

**3. Names are not required to be unique.** An intermediate proposal in this
discussion was flat URIs — `skill://<name>/SKILL.md` everywhere, with a duplicate
name across libraries a loud configuration error. It contradicts "a skill's
`name` is a label, not an identifier", and it would have made the catalogue's
correctness depend on no two upstreams ever choosing the same word. Withdrawn.
Two sources that produce the *same URI* — possible only when both feed one
library with the same path — are a real conflict: the second source fails,
named in `/health`, and serves nothing.

**4. A directory address serves nothing.** `skill://grafana/grafana-lgtm/loki` and
`skill://grafana` are directories under the spec. Reading one returns not found,
exactly as FastMCP's provider does, and the error names the index or the
`_manifest` to read instead. When FastMCP implements `resources/directory/read`,
these become real directory resources; until then they are addresses, not
content.

**5. The index resources stay, at file addresses.** An index is a small markdown
page listing skill URIs with their descriptions — this project's own addition,
not the spec's. It exists because listing every skill in `resources/list` costs
~16 k tokens per listing (§C1.18), and because until a client speaks
`skills/list` it is the only cheap way to discover a catalogue this size. It moves
off the directory addresses it occupied:

| Index | 0.0.x | Now |
|---|---|---|
| a library | `skill://grafana` | `skill://grafana/_index.md` |
| a folder of skills | `skill://grafana-lgtm` | `skill://grafana/grafana-lgtm/_index.md` |
| files outside any skill | `skill://penpot/_files` | `skill://penpot/_files.md` |

`resources/list` keeps listing the indexes, not the skills; `?skills=full` keeps
listing every `SKILL.md`. When `skills/list` arrives the indexes become the
fallback for clients without it, the same relationship the tool mirrors have to
native resources and prompts.

**6. Pack-level files keep their source-relative path under the library.**
penpot's `shared/tokens-schema.json` is `skill://penpot/shared/tokens-schema.json`.
penpot's skills cite those files as repository-root paths (`shared/…`, in
backticks) rather than as relative links, so no URI shape makes them resolve
from a skill's root; `_files.md` is how an agent finds them.

**7. A proxied MCP server's resources keep their upstream URIs verbatim.** This
supersedes the resource half of §C1.9, which namespaced them
(`n8n://workflow-sdk/reference` → `n8n://n8n/workflow-sdk/reference`). The spec's
"load by URI" rule is why: selenium-flow's instructions tell an agent to read
`skill://selenium-flow/SKILL.md`, and n8n's MCP server tells it to read
`n8n://workflow-sdk/reference`. Re-served under a prefix, both instructions would
point at nothing. The upstream already chose its URIs, and the spec treats them
as the skill's identity; an aggregator does not get to rename them. A URI claimed
by two sources is the conflict of decision 3. Prompt names keep §C1.9's
namespacing — a prompt is addressed by name, not by URI, and no upstream
instruction depends on the unprefixed form.

**8. `pack` leaves the code, as §C1.19 already decided.** `library` in every
identifier, served description, document and wiki page; the `X-Skill-Pack`
header alias is deleted with no deprecation window, because there is one
installation and no second user to protect; `index.json`'s version is bumped,
which costs one full rebuild on the first start.

**9. The shipped example demonstrates libraries and tags.** `examples/config.yaml`
gains a `libraries:` block with tags and shows `library:` and `tags:` on sources,
so the feature a reader is told about is one they can see configured.

**Deferred, and why.**

- `skills/list` and `skills/get`: wait for FastMCP or the official Python SDK to
  ship the extension (Python SDK PR #3485 is open). Implementing them here first
  would mean owning the capability negotiation and a manifest format that the
  framework will then replace.
- `resources/directory/read`: not implemented by FastMCP 4.0.x, and disabled in the
  client this was tested from.
- `_manifest`: stays in FastMCP's shape until `skills/list` carries manifests.
- Pinning `kubed-io/actions` and `duplocloud/version-bump`: they float on `@main`
  across every kubed-io repository; `kubed-io/actions` publishes no tags and
  version-bump's `main` is ahead of its newest tag. A cross-repository change for
  when both cut releases.

### §C1.24 — The plan for the next pull request: the address space

One pull request, after `mcp-kb` (#18) merges. Everything below is in scope and
nothing else is.

**Global constraints.**

- Every existing *behavioural* test is kept and its URI expectations are updated
  to the new grammar; no test is deleted to make the change pass. A test that
  asserted the old shape is rewritten to assert the new one, not removed.
- Every served URI, listing row and index body is covered by a test that reads it
  back through a real `KnowledgeBase` over MCP, not through a helper.
- Every new test is proved non-vacuous: break what it guards, watch it fail,
  restore.
- The scope rules of §C1.19 hold on every new address — an out-of-scope URI stays
  indistinguishable from a missing one, `_index.md` included.
- No backward compatibility: no redirects from 0.0.x URIs, no alias headers.

**Tasks.**

0. **What the last reviews of #18 found**, first, because two of them are about
   what a client can reach:
   - `POST /reindex` answers a failure with `str(exc)`, from an unauthenticated
     endpoint — an internal path or a remote URL can ride out in it. The caller
     gets a fixed error; the exception goes to the log.
   - A scope that names a folder does not reach pack-level files: `_admits`
     checks only that the library holds a matching skill, so a client scoped to
     one folder can read another folder's pack material by guessing its URI.
     Task 2 redefines folder scope around path prefixes; the pack-file check is
     written against that definition, with a test.
   - `catalogue/index.py` and `catalogue/snapshot.py` import `mcp.prompts`, which
     imports FastMCP, so `catalogue/` is not the FastMCP-free layer `AGENTS.md`
     says it is. The prompt row and loader move into `catalogue/`; `mcp/prompts.py`
     keeps only the provider and the tool transform. A test imports `catalogue`
     with FastMCP blocked.
   - The README and the CHANGELOG call every git scheme a shallow clone;
     `git+file://` is cloned whole, because libgit2's local transport refuses a
     shallow fetch.
   - The refresh loop ticks every 5 s while `refresh:` accepts `1s`, so a short
     interval can be late by a tick. The loop sleeps no longer than the shortest
     configured interval.
   - `.github/instructions/python.instructions.md` told reviewers `tests/` is not
     linted, and the package docstring still said "Serve Agent Skills over MCP".
     Both corrected in this task's first commit.

1. **Rename `pack` → `library`** throughout `kubed/mcp_kb/**`, tests, `AGENTS.md`,
   `.github/copilot-instructions.md`, the README and the wiki's hand-written
   pages. Delete `X-Skill-Pack` from `mcp/request.py` and every document. Bump
   `catalogue/index.py`'s `INDEX_VERSION`. Served descriptions say "library" and
   "folder", never "pack" or "group". Pure rename first, as its own commit, so
   the grammar change that follows reads cleanly.
2. **The skill URI grammar.** `catalogue/uris.py` builds and parses
   `skill://<library>/<path>/<name>/SKILL.md`, where `<path>` comes from
   `harvest` relative to the source's conventional skill root; the skill's
   files are addressed under the same prefix. `SkillIndex` resolves by full
   skill path, not by bare name. Tests: the grafana-shaped fixture (a folder of
   skills two levels down) reads back at its real path; a sibling reference
   `../other/SKILL.md` resolves to the sibling; two skills with the same name in
   different folders are both served.
3. **Directory addresses serve nothing.** A read at a skill root, a folder or a
   library returns not found; the resource read and the `read_resource` mirror
   both name `_index.md` / `_manifest` in the not-found text. Tests for each of the
   three directory kinds, through the resource and through the tool.
4. **Indexes at `_index.md` and `_files.md`.** Library index, folder index and the
   pack-level files index move to file addresses; `resources/list` lists exactly
   those; `?skills=full` lists every `SKILL.md` at its new URI. Tests pin the
   listing rows and the index bodies.
5. **URI and prompt-name conflicts fail the second source.** When two sources in
   one library produce the same skill URI, the same pack-level file URI or the
   same prompt name (`<library>_<file stem>` — two `prompts/same.md` files today
   both serve as `lib_same`, and `get_prompt` cannot tell them apart), the later
   source's record is `failed` with the conflict named, and the first keeps
   serving. Tests with two `file://` sources, one per kind of conflict.
6. **Documents and the generated wiki.** `Skills.md` rewritten on the new grammar
   with the directory, index and manifest rules; `Scoping.md`, `Installing.md`,
   `Sources.md`, the README's address-space section, `AGENTS.md`'s "surface"
   section and the CHANGELOG `[Unreleased]` lines that describe URIs, all brought
   level; `generate_wiki.py --check` green.
7. **The example.** `examples/config.yaml` with a `libraries:` block carrying tags,
   and `library:` / `tags:` on at least two sources; `tests/test_example_config.py`
   asserts the served URIs of one skill per library.
8. **The cluster installation follows after merge**, not in this pull request:
   `/projects/cluster/apps/mcp-school` renamed to `apps/mcp-kb` on the new image,
   its config gaining the same `libraries:` shape, the old Deployment and Service
   deleted by hand (a different applyset prunes nothing), and `.mcp.json` pointed
   at the new Service.

**Also in this pull request — review findings on #18 deferred to where the code
is rewritten anyway:**

- The listing emits one `skill://<group>` row per library when two libraries
  share a folder name. Removed by task 4, since folder indexes move under their
  library; task 4's listing test covers two libraries with the same folder.
- `read_resource`'s description documents only skill-owned files, not the
  pack-level forms `list_resources` returns. Rewritten with task 2's grammar.
- `list_resources` is annotated `list[dict]`; it returns rows of four strings, so
  `list[dict[str, str]]` lets a client read the row shape from `tools/list`.
- `tests/test_wiki.py`'s shadowing guard compares nested stems only against
  top-level pages, so two nested notes with the same basename pass while GitHub
  addresses both at one URL. The guard counts duplicate stems across every file.

**Proxy passthrough (decision 7) is recorded, not built** — it belongs to the
`mcp+http` epic, which has not started. This pull request only ensures nothing in
the new grammar would prevent it: a proxied URI need not begin with a library.

### §C1.25 — First contact: the deployed server, tested for real (2026-09-17)

`apps/mcp-kb` went up on the image built from #19's merge (`31f3067`) and was
driven three ways: Claude Code's own resource reader, raw JSON-RPC from inside
the pod (so no client normalised anything), and the tools-only surface
(`?resources=off&prompts=off`). Nothing here is fixed; this is the list for the
next pull request.

**What held.** The listing is 12 rows and ~2.9 KB. Every address kind reads, in
both surfaces: library, folder and `_files.md` indexes, `SKILL.md`, `_manifest`
(JSON), supporting files, library files. `..` resolves server-side, including
the upstream links that exist today (`../../grafana-k6/k6/SKILL.md` from
`synthetic-monitoring-checks`, `../checklists/typescript.md` from inside
`k6-docs/references/workflows/`). `..` past the library is clamped; `%2E%2E`,
`?query`, `#fragment`, dot-files and a wrong-case library are not found. The
directory hints are word-perfect over raw JSON-RPC and through `read_resource`.
Scoping: a library pin, a folder pin, tags, tags intersected with a library,
header over URL and `?skills=full` (102 rows) all behave as §C1.23 says. Every
list and read answers in 12–35 ms, far below anything that would hide the
server's prompts from a slash-command menu.

**Findings, most consequential first:**

1. **Penpot's skills cite library files by a root-relative path, and nothing
   resolves it.** `penpot-router/SKILL.md` lists `shared/penpot-mcp-tool-reference.md`;
   across the kit's skills there are ~220 such `shared/…` citations and no
   `../shared`. Resolved against the skill root, as SEP-2640 says, that is
   `skill://penpot/penpot-router/shared/penpot-mcp-tool-reference.md`, which is a
   bare not found. The file is at `skill://penpot/shared/penpot-mcp-tool-reference.md`.
   The `_files.md` row's description ("its skills reference them") is the only
   clue. A not-found under a skill whose tail names one of the library's files
   could say so, the same way directory reads already get a hint.
2. **In tools mode, a missing resource is a success.** `read_resource` on
   `skill://nope/x` or on a directory returns the hint as ordinary content with
   `isError: false`, while `get_prompt` on an unknown name returns `isError: true`.
   A caller that branches on the error flag (n8n's `onError`, for one) cannot
   tell "here is the file" from "there is no file". The hint text should stay;
   the flag is what should change, and the two tools should agree.
3. **A source failure is never logged.** `drive` failed at boot and again on
   `/reindex`; the pod log has no line for either. `/health` is the only place
   it shows. The loop logs `rebuilt …` on success and a traceback when a whole
   pass throws, but a source ending `failed` or `stale` is neither.
4. **A missing WebDAV folder reads as a copy bug.** With the `mcp-kb` account
   working (PROPFIND on its root is 207) and no `Agents` folder (404), `drive`
   reports `drive: copy failed: FileNotFoundError: ['']`. It should say the
   folder does not exist, and name it.
5. **First install races the LDAP account.** The pod harvested at 03:10:07; the
   `mcp-kb` ServiceAccount went Ready at 03:10:48. `drive` failed with HTTP 401
   and, being due only on its `refresh: 5m`, stayed failed for five minutes. It
   is a one-time ordering cost, but the cluster README's install steps should
   say to `/reindex` (or wait) once the account is Ready.
6. **Client mistakes log as server errors.** `get_prompt` without a required
   argument, or with an unknown name, prints a full Rich traceback at ERROR. The
   missing-argument case also answers `prompts/get` with -32603 (internal error)
   where -32602 (invalid params) is the truth. Both are FastMCP's defaults; the
   log noise is ours to silence.
7. **A pin that matches nothing is silent.** `?library=nope`, or a skill's path
   given as a library (`?library=grafana/grafana-lgtm/loki`), serves an empty
   catalogue with no word anywhere. A typo in a client's URL looks exactly like
   a server with nothing in it. The server instructions, or the empty listing's
   `list_resources` answer, could say the pin matched no library or folder.
8. **Under a folder pin, two listing rows read the same skills.**
   `?library=grafana/grafana-lgtm` lists `skill://grafana/_index.md` and
   `skill://grafana/grafana-lgtm/_index.md`, and both return the same six
   skills. One row is noise for the reader it exists to save.
9. **The unscoped `grafana` library index is ~40 KB on the wire:** all 50 skills
   with their long upstream descriptions, where its own listing row advertises
   "50 skills in 7 folders". Reading down means reading the folder index; a model
   that opens the library index first pays ~10k tokens for it. A library that has
   folders could list its folder indexes rather than every skill.

**Not ours, noted so nobody chases it.** Claude Code's resource reader throws
away the server's not-found message and prints its own "may have been deleted or
the URI is stale", so the directory hints never reach a Claude Code session;
they do reach every tools-mode client. The fix is upstream, in the host.

### §C1.26 — The plan for the next pull request: errors that say what happened

Dr K's rulings on §C1.25, 2026-09-17, and what each becomes. One pull request,
branch `error-surface`.

1. **A skill citing a library file from the source root gets a hint.** When a
   read under a skill misses and the rest of the path is one of the library's
   files, the not-found names that file's URI. The file is not also served at
   the skill-relative address: one file, one URI. Because Claude Code discards
   the server's error text, the server instructions also say that a path a skill
   cites which is not inside the skill is looked for in `_files.md`.
2. **`read_resource` reports a miss as an error.** `isError: true`, the hint as
   the message, the same as `get_prompt` on an unknown name.
3. **Logs say what the catalogue did.** Every source status change — to `ok`,
   `failed` or `stale`, conflicts included — is one line naming the source and
   its reason, at boot and on every refresh. A `LOG_LEVEL` environment variable
   (`--log-level`) sets the level for the whole process, FastMCP and uvicorn
   included, `INFO` by default.
4. **A WebDAV failure says what failed.** A folder that does not exist is
   reported as that, naming its path; 401 and 403 say the credentials were
   refused or access denied; no exception repr reaches `/health`.
5. **Refused credentials on a cold build stop the process.** A source whose
   server answers 401 or 403 while it is first built exits the process with the
   source named and the status, and Kubernetes restarts it until the account
   works — "fail till it doesn't". Only authentication, only the cold build: an
   unreachable GitHub at boot still leaves the other libraries serving (§C1.12
   stands), and a refused refresh of a source already serving makes it `stale`
   and logs an error rather than taking the catalogue down.
6. **A client's mistake with a prompt is its error, not the server's.** A missing
   required argument is -32602 naming the arguments; an unknown prompt through
   `get_prompt` says to call `list_prompts`. Neither logs a traceback: both log
   at DEBUG.
7. **A scope that names nothing is refused.** A `library` that no library in the
   config has, a folder that no served skill of that library sits under, or a
   tag nothing carries, fails the request with -32602 naming what is there — the
   client cannot connect and says why, instead of looking like an empty server.
   Scoping is chosen by whoever configures the client, not a security boundary
   against the model, so naming the libraries leaks nothing. A library whose
   sources are all down is not refused: it exists, it is only empty right now.
8. **A folder pin lists the folder, not its library.** Under
   `?library=grafana/grafana-lgtm` the library index row is dropped; it would
   read the same skills as the folder's own.
9. **Indexes list what is directly under them.** A library index lists its
   top-level folders, as their `_index.md` URIs with a skill count, and the
   skills that sit in no folder, as their `SKILL.md` URIs; a folder index does
   the same one level down, so an intermediate folder with no skills of its own
   has an index too. A library with library files lists its `_files.md`. The
   grafana library index goes from ~40 KB to a handful of lines, and the model
   decides where to go deeper. `?skills=full` stays the flat view.

## Part VI — Plugins and marketplaces

### §C1.27 — Where we are, and what the penpot links taught us (2026-09-17)

**Where things stand.** #20 merged as `f2ecbf1` and is deployed. The `drive`
source reads the Nextcloud `ai` Team folder (`ai/skills` served, `ai/agents`
waiting for agent support) as the `mcp-kb` account, which reaches it through
the `Services` team. The live server checks out. Two findings are waiting on a
pull request: `httpx` logs a line per WebDAV request at INFO (hold it at
WARNING below `LOG_LEVEL=DEBUG`), and the cluster README still says a 401
waits for the next refresh (the pod now exits).

**The penpot question.** Read as written, a penpot skill's references do not
resolve. `penpot-router/SKILL.md` cites `shared/penpot-mcp-tool-reference.md`
(frontmatter `requires:` and prose); resolved against the skill, as the Agent
Skills spec says, that is `skill://penpot/penpot-router/shared/…`, which does
not exist. #20's hint names `skill://penpot/shared/…`, and Claude Code throws
the hint away. So today a Claude Code model has to guess.

**What the kit itself says.**

- `skills.json` is penpot's own aggregate manifest (namespace, version, and per
  skill an id, path, mode and audiences). Only penpot's `validate-kit.mjs` and
  `update-lock.mjs` read it. It resembles the agentskills `skills.json`
  proposal (discussion #210), which is not in the spec.
- `skills.lock` says it outright: *"doctrine = shared/ + policies/ (vendored into
  native bundles)"*. Penpot's installer (`scripts/install/install-behavior.mjs`)
  copies `shared/` and `policies/` into every skill, and `workflows/` into
  `penpot-router`, for a native Claude Code or Codex install.
- `.well-known/agent-skills/index.json` names the Discovery v0.2.0 schema but
  carries `path`/`version` rather than the required `type`/`url`/`digest`. It
  publishes no archives.
- Penpot's `main` (0.4.0, `efbefc9`) has since added `.claude-plugin/`
  `marketplace.json` and `plugin.json`: one plugin, `source: "./"` — the whole
  repository — with `commands` pointing at its `prompts/*.md`. We pin
  `c63d8e3` (0.3.0), which predates both.

**What installing it does, tested for real.** `claude plugin marketplace add
penpot/penpot-ai-kit` then `claude plugin install penpot-ai-kit@penpot-ai-kit`
(local scope, in a scratch project, removed afterwards):

- The plugin cache is the repository copied as it stands. No build runs.
  `skills/penpot-router/` holds `SKILL.md` and `references/` only.
- The session lists 13 skills as `penpot-ai-kit:<skill>`, and the seven
  `commands` as slash commands, `/penpot-ai-kit:design-brief` and the rest.
- Asked to open the first `requires:` file, the model tried
  `…/skills/penpot-router/shared/penpot-mcp-tool-reference.md` ("File does not
  exist"), then the plugin root, `…/0.4.0/shared/…`, which worked.

Nothing resolves the reference. It works in Claude Code because the model has a
filesystem and walks up to the plugin root. That is the whole of the "support".

**Ruled by Dr K, 2026-09-17: no installer emulation.** mcp-kb does not copy
penpot's `shared/` into skills or otherwise reproduce one publisher's build
step. A rule that exists for one repository's layout is an edge-case war; the
server follows published formats instead.

### §C1.28 — The research: skills, plugins, marketplaces and the tools that install them

**Skills.** The [Agent Skills spec](https://agentskills.io/specification): a
skill is a directory, and a file reference uses *"relative paths from the skill
root"*. Penpot's skills break that in their repository form and only follow it
once installed.

**The Discovery RFC** ([cloudflare/agent-skills-discovery-rfc](https://github.com/cloudflare/agent-skills-discovery-rfc),
draft v0.2.0) is a convention, not a registry: a domain publishes
`/.well-known/agent-skills/index.json`, one entry per skill with `type`
(`skill-md` or `archive`), `url` and a `sha256` digest. An `archive` entry is
the one standard form of a finished, self-contained skill. There is no central
list; a client has to know the domain.

**Plugins.** Two formats matter, and they share the idea of a plugin root.

| | Claude Code plugin | Agent Plugins 1.0 |
|---|---|---|
| Who | Anthropic | Vercel, OpenAI, GitHub, Microsoft, Amazon, Cursor; Google joining (published 2026-08-06) |
| Manifest | `.claude-plugin/plugin.json` | `plugin.json` at the root, with its `$schema` |
| Components | `skills/`, `commands/`, `agents/`, `hooks/`, `.mcp.json`, … — the manifest may add paths | only `skills/` and `mcp.json`; anything else under a reverse-domain folder (`com.github.copilot/commands/`) |
| Plugin root | `${CLAUDE_PLUGIN_ROOT}`, substituted *anywhere* in skill and agent content | `${PLUGIN_ROOT}`, substituted only in MCP `args`/`env`/`cwd` |
| Containment | copied plugin; `../` outside it is not copied | every package path must resolve inside the plugin root |
| Marketplace | `.claude-plugin/marketplace.json` | none — installation and distribution are left to clients |

Agent Plugins has no portable commands (prompts): they are client extensions
until clients converge. VS Code detects Agent Plugins, Copilot
(`.github/plugin/plugin.json`), Claude (`.claude-plugin/plugin.json`) and legacy
OpenPlugin (`.plugin/plugin.json`). Codex reads `plugin.json` and an optional
`.codex-plugin/plugin.json`.

**Marketplaces are the de facto catalog, and they are open.** A marketplace is a
git repository with a `marketplace.json`: a `name`, an `owner`, and `plugins[]`,
each with a `name`, a `source`, and any `plugin.json` field plus `category`,
`tags` and `strict`. Claude Code reads it from `.claude-plugin/`; Copilot CLI
from `.github/plugin/` or `.claude-plugin/`; Codex from `.agents/plugins/` or
`.claude-plugin/` (Codex's own catalog shape differs: `source.path`,
`interface.displayName`). Entry `source` types in Claude Code: a relative path,
`github` (`repo`, `ref`, `sha`), `url` (git), `git-subdir` (`url`, `path`,
`ref`, `sha`), `npm`, `archive` (zip + sha256) and `command` (a directory built
by a local command, re-run each session). `strict: false` makes the marketplace
entry the plugin's whole definition. Anthropic's own
`anthropics/claude-plugins-official` lists 308 plugins, Grafana's among them
(`git-subdir` into `grafana/ai-marketplace`, pinned by `sha`); penpot is not in
it and publishes its own. Nothing in any marketplace is built: every source is
a folder in git, copied as it stands.

**Installers and the files they read or write.**

| Tool | Installs | Dependency file |
|---|---|---|
| `npx skills` (Vercel; skills.sh directory) | skill folders from git; `.well-known` indexes; archive URLs; Claude plugin manifests | `skills-lock.json`, written by the tool |
| `gh skill install` (GitHub CLI, preview) | skill folders; pins a tag or SHA; provenance in frontmatter | none |
| APM (`microsoft/apm`) | skills, plugins, prompts, MCP servers from any git host or subpath; transitive | `apm.yml` manifest + `apm.lock.yaml` with hashes |
| Claude Code / Copilot CLI / VS Code / Codex | plugins from marketplaces | client settings (below) |

No dependency file for skills is standardised. APM's pair is the most complete;
agentskills' `skills.json` + lockfile is a proposal.

**Every repository this catalogue pulls is already a marketplace.** All four
ship `.claude-plugin/marketplace.json`:

| Repository | Marketplace | Plugins |
|---|---|---|
| `grafana/skills` | `grafana-skills` (Grafana Labs) | seven, all `source: "./"` with an explicit `skills` list and keywords: `grafana-core`, `grafana-cloud`, `grafana-lgtm`, `grafana-plugins`, `grafana-app-sdk`, `grafana-k6`, `grafana-datasources` |
| `n8n-io/skills` | `n8n-io` (n8n) | one, `n8n-skills`, category `automation` |
| `obra/superpowers` | `superpowers-dev` | one, `superpowers` |
| `penpot/penpot-ai-kit` (main) | `penpot-ai-kit` | one, with seven `commands` |

Grafana's seven plugins are exactly the seven *folders* this server has been
inferring from its directory layout, each already carrying the keywords a tag
scope wants (`loki`, `tempo`, `promql`, `k6`, …). The skill that
`include.skills` had to leave out by hand, the root `template`, is in no
plugin's list. n8n, superpowers and grafana also ship `.agents/plugins/` or
`.codex-plugin/` and more; penpot ships Claude's only.

**How clients configure plugins — the UX to mirror.** Each names a marketplace,
optionally pinned, then turns plugins on as `plugin@marketplace`:

```jsonc
// Claude Code, .claude/settings.json
"extraKnownMarketplaces": {"company-tools": {"source": {"source": "github", "repo": "your-org/plugin-marketplace"}}},
"enabledPlugins": {"code-formatter@company-tools": true}
```

```jsonc
// VS Code settings.json
"chat.plugins.marketplaces": ["anthropics/claude-code"],
"chat.pluginLocations": {"/path/to/my-plugin": true}
```

```toml
# Codex config.toml
[marketplaces.company]
source = "https://github.com/your-org/plugins.git"
ref = "v1.2.0"
```

### §C1.29 — Decisions and the proposed config (recommended; open for Dr K)

1. **A plugin is what a source is.** A source is fetched once and becomes a
   *plugin root*. Its components are found from whichever manifest it has — Agent
   Plugins `plugin.json` (with the `$schema`), `.claude-plugin/plugin.json`,
   `.github/plugin/plugin.json` — or, with none, from today's conventions and
   `include`. `skills/` becomes skills; Claude `commands` and `commands/` become
   prompts; `agents/` waits for E5; `.mcp.json`/`mcp.json` waits for E6. A plain
   repository of skills is a plugin with no manifest, so nothing that works
   today stops working.
2. **A marketplace is something to fetch, and its entries are sources.** A
   marketplace is pinned once, by its `ref`, and `plugin@marketplace` names one
   of its entries, whose own `source` (relative, `github`, `url`, `git-subdir`)
   is followed and whose `sha`, when given, is honoured. `npm` and `archive`
   entries are later work; a `command` entry is never followed — running a
   publisher's build on the server is exactly the installer emulation §C1.27
   rules out.
3. **Metadata comes with the plugin.** An entry's and a manifest's `keywords`,
   `tags` and `category` join the tags of everything that plugin serves, so
   `?tags=design` reaches penpot without anyone writing it down; `description`
   and `version` appear in `/health`.
4. **Libraries compose plugins; nothing is extended.** A library lists the plugins
   it serves, by name. A plugin may be listed by two libraries. To add your own
   prompts to Grafana's plugin, a library lists both — the marketplace plugin and
   your prompts source — rather than inheriting from or overriding the entry. The
   library's `description` and `tags` are its own; the plugins' tags are added
   underneath. Shorthand: a library can take every plugin of one marketplace.
5. **A ref belongs to what is fetched.** A marketplace or a direct source carries
   its `ref`; a library never does. Two libraries sharing a plugin share one
   fetch and one commit.
6. **The address stays library-first.** `skill://<library>/<folder>/<name>/…` is
   unchanged; the plugin is provenance (in `/health`, in tags), not a URI
   segment. For every catalogue pulled today that changes no URI: Grafana's
   plugins *are* its folders, and the other three put skills directly under
   `skills/`. Two plugins of one library minting one address stay a conflict
   that fails the later one, as today.
7. **A relative reference that misses in its skill is tried against its plugin
   root.** This is not a penpot rule. It is the base both plugin formats define
   (`${CLAUDE_PLUGIN_ROOT}`, `${PLUGIN_ROOT}`), inside the containment boundary
   Agent Plugins sets, and it is what the Claude Code model did by itself in the
   live test. It is tried only on a miss, one hop, inside the same plugin and
   the same scope, and it never shadows a file the skill has. So
   `skill://penpot/penpot-router/shared/penpot-mcp-tool-reference.md` reads the
   file the kit ships, as written — and penpot's `include.files` stops being
   needed to make the links work.
8. **Dr K's URI transform idea, weighed.** A per-source find-and-replace step:
   - *Rewriting content* is rejected. It changes upstream bytes, so manifests
     and hashes describe something no longer served; penpot's references are
     frontmatter and code spans rather than links, so no parser can tell a
     reference from prose; and a pattern broad enough to catch them all will
     catch something it should not. If content substitution is ever needed, the
     safe model is Agent Plugins §9.2: exact literal tokens, one pass, never
     re-scanned — not regular expressions.
   - *Rewriting addresses* is sound in a narrow form: a rule consulted only when
     a read misses, mapping one literal prefix to another (no regex), one hop,
     inside the same library and scope, logged, and listed in `/health`. It can
     never replace a file that exists, which is the over-replacement worry
     answered. Decision 7 is exactly this rule, built in for every plugin, so
     explicit rules are deferred until a case appears that the plugin root does
     not cover.
9. **Prompt dialects join E5.** Claude commands carry `description` and
   `argument-hint` frontmatter and positional `$ARGUMENTS`; mcp-kb's prompts
   declare named `arguments` and `{{name}}`. Reading commands as prompts needs
   that dialect, which E5 already plans. Until then a plugin's commands are not
   served.

**The shape it points to** (a sketch to argue with, not a schema):

```yaml
marketplaces:                                 # catalogs, each pinned once
- name: grafana
  url: github://grafana/skills                # reads .claude-plugin/marketplace.json
  ref: 51d33e71e191b409bbd25fc7be2684c610d18166
- name: penpot
  url: github://penpot/penpot-ai-kit
  ref: efbefc935ee43804502976aa5ec9659a8bb7e207
- name: official
  url: github://anthropics/claude-plugins-official
  ref: <sha>

sources:                                      # plugins, however they arrive
- name: grafana-lgtm
  from: grafana-lgtm@grafana                  # one marketplace entry
- name: grafana-assistant
  from: grafana-assistant@official            # git-subdir into grafana/ai-marketplace
- name: penpot-ai-kit
  from: penpot-ai-kit@penpot
- name: team-notes                            # no manifest: conventions and include
  url: git+https://git.example.com/team/notes.git
  ref: main
  refresh: 1h
- name: homelab-prompts
  url: file:///srv/prompts/grafana
- name: drive
  url: webdav+http://nextcloud.cloud.svc.cluster.local:8080/remote.php/dav/files/mcp-kb/ai
  cache: live

libraries:
- name: grafana
  description: Grafana, Loki and the LGTM stack, with this homelab's prompts.
  tags: [observability]
  marketplace: grafana                        # every plugin the catalog lists …
  sources: [grafana-assistant, homelab-prompts]   # … plus these, composed in
- name: lgtm-only
  sources: [grafana-lgtm]                     # the same plugin, in a second library
- name: penpot
  sources: [penpot-ai-kit]
```

**Open questions for Dr K.**

1. `sources:` or `plugins:` as the key, now that every source is a plugin? The
   clients all say *plugin*; *source* is what this config has said so far.
2. Should a library also be able to select plugins by tag or keyword
   (`select: {tags: [grafana]}`) rather than by name? Deterministic only while
   the marketplace is pinned, and it makes a library's contents change when a
   catalog does.
3. Is the plugin root readable in full by the fallback (every non-hidden file,
   `scripts/` and `evals/` included), or only what `include.files` names?
4. A whole marketplace as one library: is that wanted, or is naming plugins one
   by one the safer default for a catalog of 308?
5. Which to align the manifest reading with first: Claude's
   `.claude-plugin/plugin.json` (penpot, Grafana, the official marketplace today)
   or Agent Plugins' `plugin.json` (the cross-vendor standard, no commands)? The
   recommendation is both, Claude's first, because every plugin we pull today
   ships Claude's.
6. Ask penpot upstream to publish `archive` entries in the index it already
   claims, or self-contained skills? Decision 7 no longer depends on it.

### §C1.30 — Second pass with Dr K: a library *is* a marketplace (2026-09-17)

**Accepted by Dr K: the plugin-root fallback** (§C1.29 decision 7), over any
regex or find-and-replace step. A link that misses inside its skill is tried
once against its plugin's root; nothing is rewritten, and a file the skill has
always wins.

**Superseded: separate `marketplaces:` and `sources:` lists** (§C1.29's sketch).
Dr K's reading: a marketplace and a library are one to one — a marketplace is
a library defined somewhere else — so the config has one concept for both, and
*plugin* replaces *source* as the thing a library holds, matching every client.

- **A marketplace is a library, and each of its entries is a plugin.** Grafana's
  marketplace is the `grafana` library with seven plugins; penpot's is a
  library with one.
- **`source` means the same word at both levels.** On a library it is where a
  `marketplace.json` lives; on a plugin it is the plugin root. A plugin's
  `source` takes any backend this server has (`file://`, `git+https://`,
  `github://`, `webdav+http(s)://`), and `skills`, `prompts` and `files` are
  paths relative to it.
- **A ref rides on the URL**, `github://owner/repo@<sha>`, pip-style. A source
  needing options (`auth`, `cache`, `refresh`) takes the long form
  `{url: …, refresh: …}`.
- **A marketplace library can be narrowed and extended in place.** Its
  `plugins` list picks entries by name (a string) and adds local plugins
  beside them (an object); left out, every entry comes along.
- **One plugin in two libraries is `name@library`**, the same form clients use
  for `plugin@marketplace`. YAML anchors were considered and dropped: they
  cannot reach a plugin that came from a marketplace, and two ways to share
  one is one too many.
- **Plugin fields are Claude's marketplace entry fields**: `name`,
  `description`, `category`, `keywords`, `tags`, `source`; plus this server's
  `skills`, `prompts` (Claude's `commands`), `files`, and later `agents`
  (E5) and `mcpServers` (E6).
- **`category` becomes a scope**, `?category=design`, beside `?library=` and
  `?tags=`; `keywords` count as tags.

The working sketch is `stuff/config.yaml` (scratch, never cited from committed
docs once the schema lands).

**Open, from this pass.**

1. `?library=grafana/grafana-lgtm` today names a *folder*. Under plugins it would
   name a *plugin* — the same string for Grafana, whose plugins are its folders,
   but not in general. Does the second segment of a scope become the plugin, or
   stay the folder?
2. A marketplace entry whose skills sit outside its `source` (Grafana's seven
   all have `source: "./"` and a `skills` list) — the plugin root is the whole
   repository, so the fallback of decision 7 reaches all of it. Accept, or
   bound the fallback to the entry's listed paths?
3. `prompts` or `commands` for the field — this server's word, or Claude's?

### §C1.31 — Third pass: named sources, plugins at the top, and selecting by tag and category (2026-09-17)

**Dr K's revision of the sketch** (`stuff/config.yaml`):

- **`sources:` are named backends**, each with its own `url`, `auth`, `cache` and
  `refresh`: `nextcloud` is `webdav+http://…/remote.php/dav/files` with its
  credentials, `github` is `git+https://github.com`. A plugin never carries
  credentials; its `source` is a path under a named backend written as a URI
  whose scheme is that backend's name — `nextcloud://mcp-kb/ai`,
  `github://penpot/penpot-ai-kit@<sha>`. `file://` needs no definition.
- **`plugins:` is a top-level list**, defined once, and libraries draw on it.
- **A git subdirectory is `//`** — `github://my-team/my-repo//foo@main` — the
  hashicorp go-getter convention kustomize and Terraform use.
- **A library can select plugins by tag**: a list of comma-separated strings,
  where the commas inside one string are AND and the list items are OR —
  `["foo,bar", "baz"]` is (foo AND bar) OR baz.

**Issues found in the revision.**

1. **`tags` on a library now means two things.** On `grafana` and `kubed` it
   *labels* everything the library serves; on `oncall` it *selects* plugins.
   One key cannot do both — `kubed: tags: [homelab]` would start pulling in
   every plugin tagged `homelab`. The selector needs its own key.
2. **The ref syntax mixes conventions.** go-getter and kustomize write
   `//subdir?ref=main`; `@main` is pip's. Taking `//` from one and `@` from the
   other makes a URL neither tool reads.
3. **`homelab-prompts` is listed by name in `grafana` while also being defined
   at the top** — fine, and exactly the `name@library` question from §C1.30
   settled the other way: plugins at the top need no `@library` at all.

**Research: how tools combine a category with tags.**

| Tool | Within one field | Across fields |
|---|---|---|
| Faceted search (the e-commerce norm) | OR — Blue *or* Red | AND — Nike *and* (Blue or Red) *and* size 10 |
| GitHub search | `label:a,b` is OR | `label:a label:b` (repeated) is AND |
| WordPress `WP_Query` | `tag=a,b` OR, `tag=a+b` AND; same for `category_name` | category and tag narrow together |
| Algolia `facetFilters` | nested array is OR | flat array is AND — `(A AND B) OR C` cannot be written |
| Kubernetes label selectors | `in (a,b)` is OR | comma-separated requirements are AND |

Two things hold everywhere. Across fields it is always AND: a category and a
set of tags narrow each other, which is the "categories first, then tags"
reading. And a field with one value per item — a Claude plugin has one
`category` — only means anything with OR: "in design *and* engineering" matches
nothing. What differs is what a comma means inside a field: OR on GitHub and
WordPress (and in this server's `?tags=` today), AND in Kubernetes selectors
and in Dr K's proposal.

**Recommendation (open for Dr K).**

- **Categories are OR, and narrow first.** A plugin has one category, so a
  list of categories can only mean any of them; `category` and `tags` then AND
  together, as every faceted search does.
- **One rule for lists and commas, the same in YAML and in the URL.** Dr K's
  rule — a comma inside one value is AND, separate values are OR — applied to
  tags and categories alike:

  ```yaml
  select:
    categories: [design, engineering]   # any of these
    tags: ["foo,bar", baz]             # (foo and bar) or baz
  ```

  ```
  ?categories=design&categories=engineering&tags=foo,bar&tags=baz
  ```

  A comma in a category (`design,engineering`) can never match a plugin and is
  refused with a message saying to list them, through the scope refusal #20
  added. This keeps one comma meaning across the whole query. The cost: it
  reverses today's `?tags=a,b` (any-of), which this server can do without a
  deprecation window; and a category list in a URL is written by repeating the
  parameter rather than with a comma.
- The alternative — Dr K's `categories=foo,bar` as OR — reads well on its own,
  but beside `tags=foo,bar` meaning AND it puts two meanings of one comma in
  one URL.

## Closing questions for Dr K

*Superseded by §C1.22 — the name, here and in question 1, is `mcp-kb`. What was
asked stands; only the word it was asked under changed.*

Answered in the second pass: the name is `mcp-school` (§C1.16); `git+https://`
stands, now argued from PEP 610 rather than taste (§C1.17); the cache gets an
index and a per-source `snapshot`/`live` dial (§C1.18); reindexing or a restart
is the update path (§C1.18).

Still open:

1. *Answered 2026-09-16 in §C1.23: `library` stays, and becomes the first segment of every skill URI.*
   **§C1.19 — is `library` the right word** for the grouping, now that the
   project is `mcp-school`? `subject`, `course` and `department` all fit the
   metaphor better, and this is the one word that appears in every URI. My
   preference is to keep `library`: it is what the thing is, and the metaphor
   should not cost a reader clarity.
2. **§C1.18 — `refresh` per source, or one server-wide interval?** Per source is
   in the schema above because a pinned git repo and a live drive folder want
   very different numbers, but it is one more knob.
3. **§C1.8 — instructions and agents as resources** under `instructions://` and
   `agent://` in E5, or cut them from this chapter?
4. **§C1.13 — who bumps pinned SHAs** now that `update-skills.yml` is gone.
   Renovate's custom regex manager can update a SHA in YAML given a `depName`
   and datasource, so a cluster-repo Renovate rule is the off-the-shelf answer;
   the alternative is floating trusted upstreams on a branch and letting
   `refresh` pick them up.
5. **§C1.13 — where `prompts/grafana/debug-logs.md` lives:** a ConfigMap-mounted
   `file://` source in the cluster repo, or a `git+https` source back at this
   repo?
6. **Order after E2** — the default is E3, E4, E5, E6, E7, E8. Any of E4, E5,
   E6 and E8 can move up once E2 lands.
