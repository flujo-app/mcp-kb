# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!--
  These ARE the release notes. One line per entry, written for someone reading
  "what's new" — never a paragraph. Length tracks impact: functional changes get
  the most words (still one line); refactors/tests stay short; CI/devops are
  shortest. Only **BREAKING:** may stretch.

  ONLY EVER EDIT THE [Unreleased] SECTION. Every section below it carries a
  version number and is IMMUTABLE — those notes shipped with a release and must
  never be reworded, reordered, or removed. Add new work under [Unreleased].
  publish.yml (duplocloud/version-bump) rolls [Unreleased] into a dated version
  section at release time.
-->

## [Unreleased]

### Added
- `git+https://`, `git+http://`, `git+file://` and `github://org/repo` sources: cloned bare and shallow with pygit2, read at `ref` (a branch, tag or commit), exported into the cache; `subdirectory` and `auth` per PEP 610.
- `mcp_school.index`: an on-disk index of what each source yielded (`Index`, `SourceRecord`, `SkillRow`, `PromptRow`), written to `<CACHE_DIR>/index.json`. The server starts from it without touching any source, then verifies each source's fingerprint (`sources.fingerprint()`, a cheap file-count/bytes/newest-mtime summary) in the background and rebuilds only what changed; a `refresh: <duration>` (`30s`, `5m`, `1h`) per source schedules those re-checks, `POST /reindex` forces one immediately, and a session that persists across requests is told the catalogue changed next time it asks — MCP 2026-07-28 has no sessions, so a sessionless client relies on the advertised `cache_ttl` instead.
- Listings are memoised per scope, so `resources/list` is a dict lookup.
- `mcp-school schema` prints the config JSON Schema.
- `mcp_school.config`: a validated config file of sources and libraries (`Config`, `FileSource`, `{env:}` secrets), published as `config.schema.json` and printable via `mcp-school schema`.
- `mcp_school.harvest` and `mcp_school.sources`: convention-based globs turn a `file://` source directory into skill dirs, prompt files and pack-level files, with a traversal guard so a glob like `../**` finds nothing.
- MCP prompts, served from `prompts/<pack>/<name>.md` with declared arguments and scoped by `X-Skill-Pack` like skills — starting with `grafana_debug-logs`, which walks the Grafana MCP server through debugging a workload's Loki logs.
- `superpowers` skill pack from `obra/superpowers` (14 skills) — brainstorming, TDD, systematic debugging, writing plans and the rest of the workflow discipline set.
- `?skills=full` (or `X-Skill-Listing: full`) enumerates every skill in the listing, for clients that sync skills to disk and can only find them by scanning for `/SKILL.md`.
- `X-Skill-Pack` request header pins a client to one pack — a ceiling the model cannot widen past, so one deployment can serve several single-pack agents.
- An `extras` key in `skills.toml`, serving files a pack ships outside its skills at `skill://<pack>/<path>` — penpot references `shared/*` from 190 places and those links were dead.
- PRs are gated on the Test and PR Tasks checks; the image no longer builds on a pull request, only on merge to main.
- An unknown `pack` no longer names the other packs in its error message, which leaked them to a pinned client.
- Split the single `server.py` into `skills.py` (catalogue), `tools.py`, `routes.py`, `server.py` (wiring) and `main.py` (entry), separating MCP wiring from tool implementations.
- `penpot` skill pack from `penpot/penpot-ai-kit` (12 skills, 108 supporting files) — pairs with the Penpot agent.
- `AGENTS.md` covering how to add a skill pack, the `kubectl build`/`up` kustomize flow, and how to ship with the publish/deploy workflows.

- MCP server serving Agent Skills, with progressive disclosure in the address space — so a client sees a dozen index rows no matter how many skills are installed.
- Indexes addressable by source (`skill://n8n`) or group (`skill://grafana-lgtm`), plus `SKILL_PACKS` to hard-scope an instance to a subset the model cannot widen.
- Skills published as `skill://` resources, mirrored as tools for clients that do not speak the resource half of MCP.
- Skill sources declared as pinned dependencies in `skills.toml` and fetched at image build time, never vendored — currently 64 skills from n8n-io/skills and grafana/skills.
- Skill discovery that walks for `SKILL.md`, so a source may nest its skills at any depth; the directory containing one becomes its selectable group.
- `GET /health` reporting status and discovered root count, wired to the Kubernetes readiness and liveness probes.
- Weekly **Update Skills** workflow that repins every source to upstream HEAD and opens a PR.
- Kubernetes manifests deploying to the `flow` namespace as `skills-mcp:8000`, unauthenticated and read-only.
- Node affinity keeping the pod off the control-plane nodes, which carry no taint in this cluster and would otherwise be scheduled onto.

### Changed
- **BREAKING:** the image bakes nothing. Sources are fetched at start into `CACHE_DIR`; `examples/config.yaml` ships the four upstream packs as `github://` sources. `skills.toml`, `scripts/fetch_skills.py`, the Update Skills workflow, the bundled prompts, the Kubernetes manifests and the Deploy workflow are gone — installation lives with the installer.
- **BREAKING:** the catalogue is a config file of sources (`CONFIG`, default `/etc/mcp-school/config.yaml`, schema in `config.schema.json`); `SKILLS_DIR`, `PROMPTS_DIR` and `SKILL_PACKS` are gone. Sources join libraries, every resource and prompt carries tags, and `/health` reports each source.
- **BREAKING:** renamed to `mcp-school` — distribution `mcp-school`, package `mcp_school`, console script `mcp-school`, image `kubed/mcp-school`, Kubernetes resources `mcp-school`. The `skill://` URIs, tools, headers and env vars are unchanged.
- **BREAKING:** the tool surface is now two tools — `list_resources()` and `read_resource(uri)` — which mirror `resources/list` and `resources/read` exactly, replacing `list_packs` / `list_skills` / `read_skill` / `read_pack_file`. A client that can drive MCP resources can drive this server without learning a second vocabulary for the same act.
- Resources are the interface and the tools are a mirror of them, so a client that reads resources is now shown no tools at all; declare `?resources=off` on the MCP URL (or `X-MCP-Resources: off`) to reveal the two, as n8n must.
- Everything is addressed by one `skill://` grammar — an index is one segment, content is two or more — so a skill, a file it references and a file its pack references are all fetched the same way.
- The image build hands a venv from builder to runner and installs dependencies before the source, so the runner no longer reinstalls every dependency out of the wheel and a source-only commit reuses the cached dependency layer.
- The image runs Python 3.14, matching the version CI gates pull requests on.
- Ruff lints the whole checkout, tests and scripts included, with the rule set selenium-flow uses.
- `python-frontmatter` replaces the two hand-written frontmatter parsers in `skills.py` and `prompts.py`; every skill and prompt now also carries its library, its source and its kind as tags.

### Fixed
- A refresh that fails now keeps the last good catalogue: the source is marked `stale` in `/health` with the error that broke it and goes on being served, where an unreachable remote used to empty it until the next successful pass.
- A source with `refresh:` is now re-checked once per its own interval instead of every 5 seconds forever once it is unchanged or fails the same way twice — the schedule tracks when a source was last *checked*, not when its record was last *built*, which only moved on an actual rebuild.
- A `PermissionError` (or any other `OSError`) reading one source now fails only that source instead of freezing the whole refresh pass over every source; `POST /reindex` answers with an error payload instead of an unhandled 500.
- The Kubernetes deployment now mounts an `emptyDir` at `CACHE_DIR` (and one at `/tmp`), so `index.json` is actually written and read in the cluster under `readOnlyRootFilesystem: true` — previously nowhere to write it, silently.
- `Index.write`'s temp file is now unique per write (`tempfile.mkstemp`) rather than a fixed sibling name, closing a cross-process race for it.
- `School.refresh` now writes `index.json` after swapping in the new snapshot rather than between the two commits, so a failure persisting it can no longer leave the served catalogue on the old generation while its records have already moved on.
- `PromptProvider.visible` reads `School.snapshot` once instead of twice, closing a narrow window where a refresh between the two reads could mix prompts from one generation with the index from the next.
- A skill's manifest no longer lists a file reached through a symlink pointing outside the skill root; reading it was already refused.
- `resources/list` answers in milliseconds instead of ~5 seconds: pack-level files are scanned once at startup rather than on every call, which also stops the listing from freezing every other request, health probes included, while it ran.
- Pack-level files are no longer hidden when the skills directory itself sits under a dot-directory such as `~/.cache`.
- `list_resources` and `read_resource` are annotated read-only; unannotated, MCP's defaults advertised them as destructive.
- `X-Skill-Pack` now scopes resources, not just tools. It filtered roots once at boot, so a pinned client could read any pack whose URI it could guess — and every URI here is guessable by design.
- Skill names no longer collide across packs: URIs are pack-qualified, where `SkillsDirectoryProvider` keyed on the folder name and silently dropped the loser entirely.
- `resources/list` is ~1.9 KB instead of 77 KB — it lists a dozen indexes rather than every skill and manifest, which is the expense this server already rejected `ResourcesAsTools` for.
- Pack-level dotfiles no longer earn a pack an index row pointing at nothing readable; grafana's only such files are two `.gitkeep` placeholders.
