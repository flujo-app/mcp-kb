# mcp-school

A resource and prompt gateway for MCP. Serves [Agent Skills](https://code.claude.com/docs/en/skills)
and prompts over MCP, so any client can discover and read them — including clients that only
speak tools.

Skills are declared as pinned dependencies in `skills.toml` and fetched at image build
time. Nothing is fetched at runtime.

```
docker run -p 8000:8000 kubed/mcp-school:latest
```

## One address space

Everything this server serves is a `skill://` URI, and reading one is the only
operation there is:

```
skill://<pack>                      an index: every skill in a pack
skill://<group>                     an index: every skill in a group
skill://<pack>/<skill>              that skill's instructions
skill://<pack>/<skill>/_manifest    what else it ships
skill://<pack>/<skill>/<path>       one of those files
skill://<pack>/_files               files the pack ships outside its skills
skill://<pack>/<path>               one of those
```

One segment is an index, two or more is content. That is the whole grammar.

Progressive disclosure lives in the address space rather than in a tool list, so the
listing stays small no matter how many skills are installed:

```
list                                  →  12 indexes, ~1.9 KB
read skill://grafana-lgtm             →  6 skills, as URIs
read skill://grafana/loki             →  the instructions to follow
```

Currently served: **90 skills** across four packs.

## Resources first, tools as a mirror

MCP has a primitive for material an agent reads, and it is the resource. So resources
are the interface, and a client that speaks them sees **no tools at all**.

Many clients only implement tools — n8n's MCP Client Tool is one — and to those a
resource-only server looks empty. They get the same interface as two tools:

| tool | mirrors |
| --- | --- |
| `list_resources()` | `resources/list` — the same `uri`/`name`/`description`/`mimeType` rows |
| `read_resource(uri)` | `resources/read` — the same URI |

Turn the mirror on with `?resources=off` on the MCP URL, or an `X-MCP-Resources: off`
header:

```
http://mcp-school.flow.svc.cluster.local:8000/mcp?resources=off
```

The two tools are hidden from clients that read resources, because advertising both
shapes is two ways to ask one question. They stay callable either way.

## Filtering to one pack

`X-Skill-Pack` pins a client to one pack or group, and it is a ceiling the model cannot
widen past — enforced on resources and tools alike:

```
X-Skill-Pack: penpot
```

Set it once in the client's connection config. In n8n that is a Header Auth credential
on the MCP Client Tool node — a plumbed constant, not something the model fills in.

A deployment that should serve less gets a config that lists less.

## Prompts

A prompt is a template a person picks and fills in before the model sees anything —
Claude Code lists them as slash commands. They live in this repo, one folder per pack:

```
prompts/grafana/debug-logs.md    →  prompt grafana_debug-logs
```

Each is YAML frontmatter declaring its arguments, then a body with `{{ placeholders }}`:

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

Double braces, because prompt bodies are full of LogQL and JSON. `X-Skill-Pack`
scopes prompts exactly as it scopes skills.

## Skills as dependencies

`skills.toml` is the source of truth:

```toml
[[source]]
name = "superpowers"
repo = "https://github.com/obra/superpowers.git"
ref  = "b36e0829c6d0140e93cfef2ca599b1b07d4a7797"
path = "skills"
```

`ref` is a commit, so an image is reproducible. `path` is the subdirectory holding the
skill folders — not the repo root. `skills/` is gitignored; upstream markdown is never
vendored into this repo, so a skill bump reviews as a one-line ref change.

A pack that factors shared material up out of its skills — penpot references `shared/*`
from 190 places — declares those directories as `extras`, and they are served at
`skill://<pack>/<path>` alongside the skills that cite them.

Fetch them locally:

```
python scripts/fetch_skills.py            # fetch at the pinned refs
python scripts/fetch_skills.py --update   # repin everything to upstream HEAD
```

The **Update Skills** workflow runs that weekly and opens a PR.

## Configuration

| variable | default | meaning |
| --- | --- | --- |
| `CONFIG` | `/etc/mcp-school/config.yaml` | config file listing the sources to serve |
| `CACHE_DIR` | `/var/cache/mcp-school` | directory a non-`file://` source materialises into |
| `TRANSPORT` | `http` | `http` or `stdio` |
| `HOST` | `0.0.0.0` | bind address |
| `PORT` | `8000` | port |

See `examples/config.yaml` for what the image ships, and `config.schema.json` — a
plain `Config.model_json_schema()` — for the full shape of a source.

Per request: `?resources=off` reveals the tool mirror, `?skills=full` enumerates every
skill in the listing (for clients that sync skills to disk), `X-Skill-Pack` pins a pack.

`GET /health` reports status, the libraries and skill/prompt counts, and each
configured source's own status.

## Deploying

```
kubectl apply -k .
```

Runs in the `flow` namespace as `mcp-school:8000`. There is no authentication: every
skill served is public markdown, the server has no write path and holds no credentials.

## Development

```
pip install -e .[test]
pytest
```

## License

MIT
