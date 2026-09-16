# mcp-school

A resource and prompt gateway for MCP. Serves [Agent Skills](https://code.claude.com/docs/en/skills)
and prompts over MCP, so any client can discover and read them — including clients that only
speak tools.

The image bakes nothing. A source is a dependency, declared in a config file and
fetched at container start — see `examples/config.yaml`, the four packs this
image used to bake, now read straight from GitHub:

```
docker run -p 8000:8000 -v mcp-school-cache:/var/cache/mcp-school \
  -v $PWD/examples/config.yaml:/etc/mcp-school/config.yaml:ro \
  kubed/mcp-school:latest
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
Claude Code lists them as slash commands. A source serves them like anything
else it ships: `prompts/**/*.md` by convention, or an explicit `include.prompts`
glob, named `<pack>_<file-stem>`.

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

## Sources

A source is a dependency, declared like one in the config file:

```yaml
sources:
- name: superpowers
  url: github://obra/superpowers
  ref: b36e0829c6d0140e93cfef2ca599b1b07d4a7797
  include:
    skills: ["skills/*/SKILL.md"]
```

| scheme | backend |
| --- | --- |
| `file://` | a directory on this machine, served in place — must be absolute (`file:///path`) |
| `git+https://`, `git+http://`, `git+file://` | a git remote, cloned bare and shallow |
| `github://org/repo` | shorthand for `git+https://github.com/org/repo.git` |
| `webdav+https://`, `webdav+http://` | a WebDAV folder — Nextcloud above all — copied into the cache |

`ref` is a branch, tag or commit; pin a commit and an image is reproducible, leave
it unset and the source tracks the remote's default branch. `subdirectory` narrows
the harvest to a path within the clone — the shipped example reaches the same
effect with `include` globs like `skills/*/SKILL.md` instead, without setting one.
`auth` supplies HTTP Basic credentials for a private remote as `{username, password:
{env: NAME}}` — GitHub wants `x-access-token` and a `GITHUB_TOKEN`-style PAT. It is
the only way in: a URL carrying its own `user:token@` is refused, because git saves
a remote URL to disk verbatim and the URL is quoted back in what `/health` reports.

One difference between the backends worth knowing: a symlink in a git repository is
served as a regular file holding the link's target as its text, where the same tree
behind `file://` leaves it out. Neither reads what the link points at.

A pack that factors shared material up out of its skills — penpot references `shared/*`
from 190 places — adds those directories to `include.files`, and they are served at
`skill://<pack>/<path>` alongside the skills that cite them.

A git source is cloned once and re-exported on its `refresh` interval (`30s`, `5m`,
`1h`); left unset, it is read once at boot and only rebuilt on restart.

### A WebDAV folder, and `cache: live`

The URL *is* the folder, so a Nextcloud share is the path you see in its web UI
under `remote.php/dav/files/<user>/`. `auth` is required — WebDAV has no useful
anonymous mode — and the password is an `{env:}` reference to a Nextcloud **app
password**, never the account's own:

```yaml
- name: notes
  url: webdav+https://cloud.example.com/remote.php/dav/files/drk/Skills
  auth:
    username: drk
    password: {env: NEXTCLOUD_APP_PASSWORD}
  cache: live
  refresh: 5m
  include:
    skills: ["*/SKILL.md"]
```

`cache` is the dial. `snapshot`, the default, is every other mirrored source: the
folder is copied at index time and read from disk thereafter, so a request touches
no network at all. `cache: live` keeps that copy and revalidates a file as it is
read — one PROPFIND for its ETag, and a download only if the ETag moved. **A file
edited in Nextcloud is served on the next read**, with no refresh and no restart.

What live mode does *not* do is notice a **new** file. One that did not exist when
the folder was indexed has no URI, so nothing ever asks to read it — and the same
goes for a renamed or deleted one. That is what `refresh: 5m` above is for, and
`POST /reindex` forces it now. Live mode is for the files that are already there.

A server that stops answering is not an outage either: a revalidation that fails is
logged and the read is served from the copy on disk.

One caveat if the folder is not Nextcloud's. Revalidation is only as sharp as the
server's ETag — Nextcloud derives one from the content, but a server that derives it
from mtime and size can miss an edit that changed neither, and one that sends no
ETag at all falls back to exactly that pair. `cache: snapshot` plus a `refresh`
interval is the honest setting there.

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

## Index and refresh

`CACHE_DIR/index.json` is the on-disk catalogue: what each source yielded last
time, rows enough to rebuild every skill and prompt without re-reading a
single file. Cold start reads it and serves in milliseconds; a background pass
then checks each source's fingerprint and rebuilds only the ones that moved,
so a pod restart never has to re-harvest a source that has not changed.

`refresh: 5m` on a source schedules that check; a source with none is never
revisited on its own:

```yaml
- name: superpowers-tip
  url: github://obra/superpowers
  ref: main
  refresh: 5m
```

`POST /reindex` forces a rebuild of every source immediately, fingerprint
check skipped, and answers with `/health`'s body plus `rebuilt`, the source
names actually rebuilt this pass — since the fingerprint check is skipped,
that is every source that did not fail identically to how it already had,
changed or not, not only the ones whose content moved.

A session that persists across requests is told, once, the next time it asks
— MCP 2026-07-28 has no sessions of its own, so a sessionless client gets no
such notice and instead relies on `cache_ttl`, which this server advertises as
the shortest `refresh` among its sources.

## Deploying

The image is the whole artifact: point `CONFIG` at a config file and `CACHE_DIR` at a
writable volume, and it serves whatever the config names. Kubernetes manifests, node
placement and everything else about running this somewhere are the installer's
concern — this repo ships none of its own.

`CACHE_DIR` has to be writable by uid **65534**, which the container runs as. A Docker
named volume inherits the directory's ownership and needs nothing; a Kubernetes
`emptyDir` or PVC mounts root-owned, so the pod needs `securityContext.fsGroup: 65534`.
Without it the server logs that it cannot write `index.json` and re-harvests every
source on every restart.

## Development

```
pip install -e .[test]
pytest
```

## License

MIT
