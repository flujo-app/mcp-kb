# 📚 mcp-kb

**An MCP knowledge base.** Skills, prompts and agent material collected from git, WebDAV and folders into one catalogue, served as resources — or as tools, for clients without them. 🗂️

[![🧪 Test](https://github.com/kubed-io/mcp-kb/actions/workflows/test.yml/badge.svg)](https://github.com/kubed-io/mcp-kb/actions/workflows/test.yml)
[![🛡️ Quality](https://github.com/kubed-io/mcp-kb/actions/workflows/quality.yml/badge.svg)](https://github.com/kubed-io/mcp-kb/actions/workflows/quality.yml)
[![📸 Image Builder](https://github.com/kubed-io/mcp-kb/actions/workflows/image.yml/badge.svg)](https://github.com/kubed-io/mcp-kb/actions/workflows/image.yml)
[![📖 Wiki](https://github.com/kubed-io/mcp-kb/actions/workflows/wiki.yml/badge.svg)](https://github.com/kubed-io/mcp-kb/wiki)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-kubed%2Fmcp--kb-2496ed?logo=docker&logoColor=white)](https://hub.docker.com/r/kubed/mcp-kb)
[![FastMCP](https://img.shields.io/badge/FastMCP-4-8a2be2)](https://gofastmcp.com/)

---

## The whole idea, in one breath

Your [Agent Skills](https://code.claude.com/docs/en/skills) and prompts are scattered — a plugin marketplace on GitHub, a folder somebody edits in Nextcloud, a ConfigMap in the cluster. Name them in one config file and every agent reads them from one address space, over MCP.

```
sources (git · WebDAV · folders)  ──▶  plugins  ──▶  libraries  ──▶  agents (resources, or tools)
```

**The image bakes nothing.** A source is a dependency — a URL and a credential that is a *reference* to an environment variable rather than a value — and a plugin is a folder under one, pinned to a commit, fetched at container start into a cache volume. Point a library at somebody's `marketplace.json` and its plugins arrive already split, with the categories and tags they publish. 🪄

---

## 🔤 One address space

Everything served is a `skill://` URI, and reading one is the only operation there is. The grammar follows the [MCP Skills extension](https://modelcontextprotocol.io/extensions/skills/overview): the library and folder are a prefix, and the last segment before the file is always the skill's name.

```
skill://<library>/_index.md                  an index: the library's folders and skills
skill://<library>/<folder>/_index.md         the same, one folder down
skill://<library>/<folder>/<skill>/SKILL.md  that skill's instructions
skill://<library>/<folder>/<skill>/_manifest what else it ships
skill://<library>/<folder>/<skill>/<path>    one of those files
skill://<library>/_files.md                  what the library ships outside its skills
skill://<library>/<path>                     one of those files
```

A library, a folder or a skill's own directory is not a file — reading one is not found, and the error names the file to read instead. Progressive disclosure lives in the addresses rather than in a tool list, so a listing is a dozen index rows whether the catalogue holds nine skills or ninety.

📖 [Skills](https://github.com/kubed-io/mcp-kb/wiki/Skills)

---

## 🪞 Resources first, tools as a mirror

MCP already has a primitive for material an agent reads, and it is the **resource**. So resources are the interface, and a client that speaks them sees *no tools at all*.

Many clients only implement tools — n8n's MCP Client Tool above all — and to those a resource-only server looks empty. They get the same interface as four tools, revealed with `?resources=off`, `?prompts=off`, or both:

| Tool | Mirrors |
|---|---|
| `list_resources()` | `resources/list` — the same `uri`/`name`/`description` rows |
| `read_resource(uri)` | `resources/read` — the same URI |
| `list_prompts()` | `prompts/list` — each prompt's name, description and arguments |
| `get_prompt(name, arguments)` | `prompts/get` — the rendered, role-tagged messages |

The vocabulary is the protocol's own, so an agent that can drive MCP resources can drive these with nothing new to learn. Each pair is hidden from a client that has the real feature, and stays callable either way.

📖 [Tools](https://github.com/kubed-io/mcp-kb/wiki/Tools) · [Installing](https://github.com/kubed-io/mcp-kb/wiki/Installing)

---

## 🎯 One server, many narrow agents

A client can be given part of the catalogue and no more, and the narrowing is a **ceiling the model cannot widen past** — enforced on resources, prompts and every mirror tool alike:

| On the MCP URL | Header | Sees |
|---|---|---|
| `?library=grafana` | `X-Skill-Library` | that whole library |
| `?categories=observability` | `X-Skill-Categories` | every plugin declaring that category, across libraries |
| `?tags=oncall,runbooks` | `X-Skill-Tags` | what carries **both** tags; repeat the parameter for any-of |
| `?library=grafana&tags=oncall` | all three | the tagged part of that one library |

A header beats the URL, which is what makes a scope pinned inside a credential something the caller cannot edit away. It is a plumbed constant, never a tool argument — there is no way for a model to ask for material it was not given.

📖 [Scoping](https://github.com/kubed-io/mcp-kb/wiki/Scoping)

---

## 🔌 Sources, plugins, libraries

Three lists. A **source** is a backend named once. A **plugin** is a folder of skills and prompts at a path under it. A **library** is the first segment of every URI, and is a marketplace, a list of plugins, or a query:

```yaml
sources:
- name: github
  url: git+https://github.com

plugins:
- name: house-prompts
  category: operations
  source: file:///srv/prompts
  prompts: ["*.md"]

libraries:
- name: grafana
  source: github://grafana/skills?ref=51d33e71e191b409bbd25fc7be2684c610d18166
  plugins: [house-prompts]
```

| Scheme | Backend |
|---|---|
| `file:///path` | a directory on this machine, served in place — ConfigMap mounts included |
| `git+https://`, `git+http://`, `git+file://` | a git remote, cloned bare — shallow over the network, whole for `git+file://` (libgit2's local transport refuses a shallow fetch) — and exported at a commit |
| `webdav+https://`, `webdav+http://` | a WebDAV folder — Nextcloud above all — copied into the cache |

A plugin's address is `<source>://<path>[//<subdir>][?ref=<ref>]`, go-getter's grammar, the one kustomize reads. Pin a `?ref=` and the image serves the same bytes in a year; track a branch, give the source a `refresh:`, and something goes and looks. `cache: live` on a WebDAV source revalidates a file by ETag as it is read, so **a file edited in Nextcloud is served on the next read**. A credential lives on the source and is always an `{env: NAME}` reference — a URL carrying its own `user:token@` is refused.

Nothing about a marketplace's plugin is editable here: to change one, declare your own plugin against the same repository, which costs no second clone.

📖 [Plugins](https://github.com/kubed-io/mcp-kb/wiki/Plugins) · [Sources](https://github.com/kubed-io/mcp-kb/wiki/Sources) · [Configuration](https://github.com/kubed-io/mcp-kb/wiki/Configuration)

---

## 💬 Prompts

A skill is read by the model when it decides to. A **prompt** is picked by a person, who fills in a few arguments first — Claude Code lists them as slash commands. Point the server at a Claude Code command, a Copilot `.prompt.md` or one of its own files and **the same MCP prompt comes out**, with the same libraries, tags and scopes as everything else:

```markdown
---
description: Debug a workload's recent logs in Loki with the Grafana MCP server.
arguments:
- name: app
  required: true
- name: since
  default: 1h
---
Investigate the logs of **{{ app }}** over the last {{ since }}.
```

Double braces, because prompt bodies here are full of LogQL and JSON. A Claude command's `$ARGUMENTS` and a Copilot file's `${input:name:hint}` become arguments the same way. `${CLAUDE_PLUGIN_ROOT}` is rendered as an address you can read; `${selection}` and friends name things only a client has, so they are served exactly as written, and nothing fetched is ever executed.

📖 [Prompts](https://github.com/kubed-io/mcp-kb/wiki/Prompts)

---

## 🚀 Running it

### Docker


Docker must be installed and running. This single-line command works in
PowerShell, Command Prompt, and POSIX shells, from any directory:

```text
docker run --name mcp-kb -d -p 127.0.0.1:8000:8000 --mount type=volume,source=mcp-kb-cache,target=/var/cache/mcp-kb kubed/mcp-kb:latest
```

No clone or host config is needed. The image includes the example config with
four pinned public GitHub libraries. The first start downloads those libraries
before binding its port. `docker logs -f mcp-kb` shows progress; once ready,
`http://localhost:8000/health` reports what loaded and the MCP URL is
`http://localhost:8000/mcp`.

Stop it with `docker stop mcp-kb`; restart that same container with
`docker start mcp-kb`. To replace its startup options, stop it and remove the
container with `docker rm mcp-kb` before running a new command. Its named cache
volume is retained.

#### Use your own config

Clone the repository and run the following commands from its root, where
`examples/config.yaml` exists. Edit that file to choose your libraries, or use
the path to your own existing YAML file. Stop and remove the quick-start
container first if you used the name `mcp-kb` above.

**PowerShell:**

```powershell
if (-not (Test-Path -LiteralPath ./examples/config.yaml -PathType Leaf)) { throw 'Create a config YAML file first, or run from the repository root.' }
$configPath = (Resolve-Path -LiteralPath ./examples/config.yaml).Path
docker run --name mcp-kb -d -p 127.0.0.1:8000:8000 --mount type=volume,source=mcp-kb-cache,target=/var/cache/mcp-kb --mount "type=bind,source=$configPath,target=/etc/mcp-kb/config.yaml,readonly" kubed/mcp-kb:latest
```

**Bash / POSIX shell:**

```bash
if [ -f ./examples/config.yaml ]; then
  docker run --name mcp-kb -d -p 127.0.0.1:8000:8000 \
    --mount type=volume,source=mcp-kb-cache,target=/var/cache/mcp-kb \
    --mount "type=bind,source=$PWD/examples/config.yaml,target=/etc/mcp-kb/config.yaml,readonly" \
    kubed/mcp-kb:latest
else
  printf '%s\n' 'Create a config YAML file first, or run from the repository root.' >&2
fi
```

The host config must be a **file**. Docker's `-v` creates a missing source as a
directory, even when its name ends in `.yaml`; mounting that directory over the
image's config file then fails with "not a directory". `--mount` instead reports
a missing source immediately. If a previous attempt created a directory called
`config.yaml`, use a real YAML file at the intended path.

The config is read-only; the cache is not, and wants to be a **named volume**
rather than a bind mount — a named volume inherits the image's ownership of
`/var/cache/mcp-kb` and needs nothing further, where a host directory arrives
owned by whoever made it.

The container runs as uid **65534**; a custom cache directory must be writable
by that user. A named volume handles this automatically.

For private sources, add `-e GITHUB_TOKEN` before the image name to pass a
variable through for an `{env: GITHUB_TOKEN}` reference in the config. The
public example needs no token. Never put a credential in a source URL; the
server refuses one that carries it.

### Python without Docker

Use Python 3.11 or newer. Clone the repository and create an isolated environment
(skip the first two commands if you are already in the checkout):

```text
git clone https://github.com/kubed-io/mcp-kb.git
cd mcp-kb
python -m venv .venv
```

In **PowerShell** (no environment activation required):

```powershell
./.venv/Scripts/python.exe -m pip install .
./.venv/Scripts/mcp-kb.exe --config ./examples/config.yaml --cache-dir ./.cache/mcp-kb --host 127.0.0.1
```

In a **POSIX shell**:

```bash
./.venv/bin/python -m pip install .
./.venv/bin/mcp-kb --config ./examples/config.yaml --cache-dir ./.cache/mcp-kb --host 127.0.0.1
```

If you already installed the release wheel, run `mcp-kb` with those same
`--config`, `--cache-dir`, and `--host` arguments. If you copied the config to
the checkout root, use `--config ./config.yaml` instead. The CLI does not search
the current directory: its defaults remain `/etc/mcp-kb/config.yaml` and
`/var/cache/mcp-kb`, including on Windows. Both flags are therefore explicit in
the native commands.

This serves the same HTTP endpoint as Docker. Stop the Docker instance first,
or add `--port 8001` to the native command to run both. For a client that launches
the server over pipes, add `--transport stdio` instead.

📖 [Deployment](https://github.com/kubed-io/mcp-kb/wiki/Deployment) · [Installing](https://github.com/kubed-io/mcp-kb/wiki/Installing)

---

## ⚙️ Configuration

Every flag has an environment fallback: containers are configured with env vars, developers reach for flags. These say **how** to run; the config file says **what** to serve.

| Env | Flag | Default | Notes |
|---|---|---|---|
| `CONFIG` | `--config` | `/etc/mcp-kb/config.yaml` | The config file listing the sources to serve |
| `CACHE_DIR` | `--cache-dir` | `/var/cache/mcp-kb` | Where a non-`file://` source materialises, and where `index.json` lives |
| `TRANSPORT` | `--transport` | `http` | `http` or `stdio` |
| `HOST` | `--host` | `0.0.0.0` | Bind address, http only |
| `PORT` | `--port` | `8000` | Port, http only |

Per request, on the MCP URL — each with a header form that beats it:

| Parameter | Header | Does |
|---|---|---|
| `?resources=off` | `X-MCP-Resources` | Reveals the resource mirror tools |
| `?prompts=off` | `X-MCP-Prompts` | Reveals the prompt mirror tools |
| `?library=<name>` | `X-Skill-Library` | Restricts this client to one library, by name |
| `?categories=<name>` | `X-Skill-Categories` | Restricts it to the plugins declaring that category |
| `?tags=a,b` | `X-Skill-Tags` | Restricts it to what carries all of those tags; repeat for any-of |
| `?skills=full` | `X-Skill-Listing` | Lists every skill, for clients that sync them to disk |

`mcp-kb schema` prints the config JSON Schema, which is also committed as `config.schema.json` for an editor to validate against live.

📖 [Configuration](https://github.com/kubed-io/mcp-kb/wiki/Configuration)

---

## 🩺 Operations

`GET /health` reports the generation being served, the skill and prompt counts, and three maps: the **libraries** and what each serves, the **plugins** and what each yielded, and the **fetches** — one per materialised tree — each `ok`, `stale` or `failed`. It needs no credentials and answers 200 whenever the process is serving, *including* when a fetch failed to load: one unreachable remote must not take a working catalogue down.

`POST /reindex` rebuilds every fetch now and answers with the same body plus what it rebuilt. `GET /openapi.yaml` is the machine-readable contract for both.

📖 [Operations](https://github.com/kubed-io/mcp-kb/wiki/Operations) · [Endpoints](https://github.com/kubed-io/mcp-kb/wiki/Endpoints)

---

## 🛠 Contributing

Setup, the layout, what is generated and what is not, and what CI will say about
it: [CONTRIBUTING.md](CONTRIBUTING.md). The rules worth reading before changing
behaviour are [AGENTS.md](AGENTS.md), and why they are what they are is
[`saga/`](saga/).

---

## 🔗 References

- [Model Context Protocol](https://modelcontextprotocol.io/) · [FastMCP](https://gofastmcp.com/)
- [Agent Skills](https://code.claude.com/docs/en/skills) · [the skill format](https://code.claude.com/docs/en/skills#skill-structure)
- [The wiki](https://github.com/kubed-io/mcp-kb/wiki) · [Docker Hub](https://hub.docker.com/r/kubed/mcp-kb)

---

## 📜 Licence

MIT. See [LICENSE](LICENSE).
