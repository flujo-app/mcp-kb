# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!--
  These ARE the release notes. One SHORT line per entry, written for a user —
  never a paragraph. Say what someone can now do, not how it was built and not
  why. Only **BREAKING:** may stretch. Internal work — CI, refactors, tests,
  types, docs — usually earns no line at all, and never more than a terse one.
  Deeper detail lives in AGENTS.md or the PR, not here.

  A version may open with a short preamble above its first heading, framing the
  release as a whole. That is prose, and the "never a paragraph" rule does not
  reach it — it is about the entries.

  ONLY EVER EDIT THE [Unreleased] SECTION. Every section below it carries a
  version number and is IMMUTABLE — those notes shipped with a release and must
  never be reworded, reordered, or removed. Add new work under [Unreleased].
  publish.yml (duplocloud/version-bump) rolls [Unreleased] into a dated version
  section at release time. See CONTRIBUTING.md / AGENTS.md.
-->

## [Unreleased]

The first release. **mcp-kb** is an MCP knowledge base: skills, prompts and agent
material collected from git, WebDAV and folders into one catalogue, served as MCP
resources — or as tools, for the clients that have none.

The image bakes nothing. One config file names the sources, the server reads them
at start into a cache volume, and every agent reads them from one address space.

### Added

- **A config file of sources and libraries** — `CONFIG`, `/etc/mcp-kb/config.yaml` by default — where a credential is an `{env: NAME}` reference and never a value. `mcp-kb schema` prints its JSON Schema, and `config.schema.json` ships in the repository for an editor to validate against as you type.

- **`file://` sources**: a directory on this machine, served in place — including a Kubernetes ConfigMap mount, which is symlinks all the way down.

- **Git sources**: `github://org/repo`, `git+https://`, `git+http://` and `git+file://`, cloned bare — shallow over the network, whole for `git+file://` — and exported at a branch, tag or commit, with `subdirectory` and HTTP Basic `auth` for a private remote.

- **WebDAV sources**: `webdav+https://` and `webdav+http://` point at the folder itself — a Nextcloud share, with an app password as `{env:}` — and it is copied into the cache and never downloaded again while its ETags agree.

- **`cache: live` on a WebDAV source** revalidates a file by ETag as it is read, so a file edited in Nextcloud is served on the next read with no refresh and no restart.

- **`include` globs per kind** decide what counts — skills, prompts and the files a library ships outside its skills — with conventions where a kind is unset. A skills glob may name the directory (`skills/*`) as well as the `SKILL.md`.

- **A cold start in milliseconds.** `index.json` in the cache records what each source yielded, so a restart serves from it without touching a source, then verifies each fingerprint in the background and rebuilds only what moved.

- **`refresh: 30s | 5m | 1h` per source** schedules that check, and `POST /reindex` forces a rebuild of everything now.

- **The `skill://` address space**: one segment is an index, two or more is content. `skill://<library>` and `skill://<group>` are indexes, `skill://<library>/<skill>` is the instructions, `/_manifest` is what else that skill ships and `/_files` is what its library ships outside its skills.

- **Prompts as slash commands**: a markdown file whose frontmatter declares its arguments and whose body carries `{{ placeholders }}`, exposed as `<library>_<file stem>`.

- **Four mirror tools for clients without resources or prompts** — `list_resources`, `read_resource`, `list_prompts`, `get_prompt` — revealed by `?resources=off` and `?prompts=off` (or the `X-MCP-Resources` / `X-MCP-Prompts` headers) and hidden from every client that has the real feature.

- **`?library=` and `?tags=`, or `X-Skill-Library` / `X-Skill-Tags`**, narrow a client to one library or to anything carrying any of the tags — a ceiling the model cannot widen past, enforced on resources, prompts and every mirror tool alike. A header beats the URL.

- **`?skills=full`** (or `X-Skill-Listing: full`) lists every skill rather than the indexes, for clients that sync skills to disk by scanning for `/SKILL.md`.

- **`GET /health`** reports the generation, the libraries, the skill and prompt counts and each source's own status — `ok`, `stale` or `failed` — and answers 200 whenever the process is serving, so one unreachable remote never takes a working catalogue down.

- **`GET /openapi.yaml`** publishes the HTTP surface as OpenAPI 3.1, generated from the code that serves it.

- **The `kubed/mcp-kb` image** is the whole artifact: two mounts, a config file and a writable cache. The [wiki](https://github.com/kubed-io/mcp-kb/wiki) is the manual.
