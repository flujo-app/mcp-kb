# Working on skills-mcp

An MCP server that serves Agent Skills — `SKILL.md` packages — over HTTP so
clients that cannot read a filesystem (n8n agents, above all) can still use
them. Skills are **fetched at build time** from pinned upstream repos and baked
into the image; nothing is fetched at runtime.

## Layout

| Path | What it is |
| --- | --- |
| `skills.toml` | the pack manifest — the source of truth for what gets served |
| `scripts/fetch_skills.py` | clones each pinned source at build time; stdlib only |
| `kubed/skills_mcp/skills.py` | the catalogue — `Skill`, loading, and `SkillIndex` |
| `kubed/skills_mcp/uris.py` | the `skill://` address space — `Catalogue`, the grammar |
| `kubed/skills_mcp/resources.py` | the resources, and the mirror-hiding middleware |
| `kubed/skills_mcp/tools.py` | the two mirror tools |
| `kubed/skills_mcp/request.py` | what the current request says about itself |
| `kubed/skills_mcp/routes.py` | plain HTTP endpoints (`/health`) |
| `kubed/skills_mcp/server.py` | `SkillsMCP` — wiring only, no tool bodies |
| `kubed/skills_mcp/main.py` | CLI and env parsing; the only file reading `os.environ` |
| `deploy/` | raw Deployment + Service |
| `kustomization.yaml` | the one kustomization; `newTag` is the deployed version |
| `skills/` | **gitignored** — build output, never commit it |

## Adding a skill pack

The manifest is the only file you edit. A pack is a dependency, pinned like one.

```toml
[[source]]
name = "penpot"                                        # becomes the pack name
repo = "https://github.com/penpot/penpot-ai-kit.git"
ref  = "c63d8e3717323fad859e794848e5a602b155a7ec"      # a commit SHA
path = "skills"                                        # dir CONTAINING skill folders
```

Two things go wrong here, both silently:

- **`path` must be the directory that *contains* skill folders, never the repo
  root.** `grafana/skills` ships a `template/SKILL.md` at the top level that
  would otherwise be served as a skill named "template".
- **Pin a SHA, not a branch.** None of these upstreams tag releases. A branch
  ref makes the image irreproducible and the pack can change under you.

Then verify locally before pushing:

```bash
python scripts/fetch_skills.py --out /tmp/skills   # clone the packs
SKILLS_DIR=/tmp/skills python -m pytest -q         # 14 tests
SKILLS_DIR=/tmp/skills skills-mcp --transport stdio
```

Nesting depth does **not** matter. `load_skills` walks for `SKILL.md`, so a flat
source (`<pack>/<skill>/SKILL.md`, n8n) and a nested one
(`<pack>/<group>/<skill>/SKILL.md`, grafana) both work. The directory that
*contains* a skill becomes its `group`, which is why grafana has seven
selectable groups and n8n has none worth naming.

Bumping existing packs is the `🧠 Update Skills` workflow — Mondays 09:00 UTC,
or dispatch it. It repins every source to upstream HEAD and opens a PR, because
a bump is new upstream *instruction* content and deserves a read before it ships.

## Shipping a change

Two workflows, in this order. Neither runs on a push to main.

**1. `🧬 Publish Version`** (`workflow_dispatch`) — five jobs:

```
test    → the full 3.10 → 3.14 matrix; gates everything below
version → pins kustomization.yaml newTag, rolls CHANGELOG, commits + tags main
image   → checks out that tag, builds and pushes kubed/skills-mcp:vX.Y.Z
package → checks out that tag, builds the sdist + wheel as a GHA artifact
release → downloads that artifact and cuts the GitHub Release
```

Run it once with **`push: false`** first. That is a real dry run: it runs the
tests, computes the next version, and builds both the image and the package
without pushing any of them. Then run with `push: true`. The dry run is what
stops a successful tag from stranding on a failed build.

The pin happens *before* the tag, so the tag's tree already references its own
image. This is the only workflow that writes `newTag`, and it only ever writes a
semver — so main always sits on something deployable.

**2. `🚀 Deploy`** (`workflow_dispatch`) — runs `kubectl up` against this repo.

`image.yml` on its own only builds. A push to main publishes `:main` and
`:latest` for testing; it never changes what is deployed.

## Kustomize: build and up

These are this project's custom kubectl commands, not upstream kubectl. They all
take a directory — the one holding `kustomization.yaml`, which here is the repo
root. Run them from anywhere; pass the dir explicitly.

```bash
kubectl build <dir>   # render the manifests to stdout — no cluster contact
kubectl diff -k <dir> # read-only diff of the render against the live cluster
kubectl up <dir>      # apply, with applyset pruning
kubectl down <dir>    # tear the app back down
```

From the repo root that is `kubectl build .` and `kubectl up .`.

Use them in that order. `build` answers "did kustomize produce what I meant?"
and is the right check after touching `deploy/` or `kustomization.yaml`; it
never contacts the cluster. `kubectl diff -k` is the read-only preview and is
safe to run anytime. `up` is what `deploy.yml` runs, so a clean local diff is a
faithful preview of what the workflow will do.

`kubectl plan` is documented in the cluster repo's CLAUDE.md but is **not
installed in the codeserver pod** — `kubectl plugin list` shows `build`, `up`,
`down`, and friends, with no `plan`. Use `kubectl diff -k` there instead.

There is one kustomization, at the top level. `deploy/` holds the raw
Deployment and Service and has no kustomization of its own — `kubectl build
deploy` will not work, and is not meant to.

## Things that already cost someone an afternoon

- **`USER` in the Dockerfile must be numeric.** With `runAsNonRoot: true` the
  kubelet refuses a non-numeric user — `image has non-numeric user (nobody),
  cannot verify user is non-root` — and the pod sits in
  `CreateContainerConfigError`. It is `USER 65534`, pinned again as `runAsUser`
  in the pod spec.
- **The control-plane nodes carry no taint in this cluster.** Without the node
  affinity in `deploy/deployment.yaml` the scheduler will put this pod on an
  etcd/master node; it did exactly that on the first rollout.
- **`imagePullPolicy: Always` pairs with a floating tag.** While `newTag` is
  `latest`, `IfNotPresent` pins a node to whatever layer it cached first. Once
  `publish.yml` pins a semver this is just a cheap registry check.
- **Do not reach for `ResourcesAsTools`, or back for `SkillsDirectoryProvider`.**
  Both enumerate every skill on every listing call — `resources/list` was 77KB
  for this catalogue before `Catalogue` replaced it with a dozen indexes.
  `SkillsDirectoryProvider` also keys a skill on its folder name alone, so two
  packs shipping a `testing/` collapse into one and the loser vanishes from the
  server entirely. Neither failure raises anything.

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
- **A pack references files outside its skills** → add them to that source's `extras`
  in `skills.toml`. They are served at `skill://<pack>/<path>` and listed under
  `skill://<pack>/_files`, never indexed as skills. The spec says a skill is
  self-contained, so most sources need none — grep the SKILL.md files for
  `shared/`-style paths before reaching for it.
- **Changing what counts as a skill, or who may see one** → `skills.py` for the
  rule, `uris.py` for where it is applied. Every `Catalogue` method takes
  `pinned`, so there is no method that can be called without deciding about the
  scope — a handler that reimplemented that filter is how a pinned client ends
  up seeing another pack.
- **A new flag or env var** → `main.py`, which is the whole configuration
  surface. Nothing else in the package reads `os.environ`.

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
  Prefer this — a second Deployment is not free here, because the kustomization
  sets `includeSelectors: true`, so a second instance would inherit the same
  selector and the existing Service would load-balance across both. Fixing that
  means changing `spec.selector`, which is immutable and forces a recreate.
- **`SKILL_PACKS` env**, per deployment. Narrower blast radius but a whole pod.

They compose: the header narrows within whatever `SKILL_PACKS` allows. Header
scoping only exists inside an HTTP request, so it is inert over stdio — the
tests in `tests/test_header_scope.py` run a real uvicorn server for that reason.

## Verifying in the cluster

```bash
kubectl -n flow rollout status deploy/skills-mcp
kubectl -n flow port-forward svc/skills-mcp 18000:8000
curl -s localhost:18000/health   # {"status":"ok","packs":[...],"skills":N}
```

`/health` reports the packs and skill count, which is the fastest way to tell
which image a pod is actually running.

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
- No `Makefile`. Everything is `pyproject.toml` plus the two scripts.
- No auth on this server, by design: every skill it serves is public markdown
  that is already on GitHub. Do not add a token without a reason to.
