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

### Changed
- **BREAKING:** the tool surface is now two tools — `list_resources()` and `read_resource(uri)` — which mirror `resources/list` and `resources/read` exactly, replacing `list_packs` / `list_skills` / `read_skill` / `read_pack_file`. A client that can drive MCP resources can drive this server without learning a second vocabulary for the same act.
- Resources are the interface and the tools are a mirror of them, so a client that reads resources is now shown no tools at all; declare `?resources=off` on the MCP URL (or `X-MCP-Resources: off`) to reveal the two, as n8n must.
- Everything is addressed by one `skill://` grammar — an index is one segment, content is two or more — so a skill, a file it references and a file its pack references are all fetched the same way.

### Fixed
- `X-Skill-Pack` now scopes resources, not just tools. It filtered roots once at boot, so a pinned client could read any pack whose URI it could guess — and every URI here is guessable by design.
- Skill names no longer collide across packs: URIs are pack-qualified, where `SkillsDirectoryProvider` keyed on the folder name and silently dropped the loser entirely.
- `resources/list` is ~1.9 KB instead of 77 KB — it lists a dozen indexes rather than every skill and manifest, which is the expense this server already rejected `ResourcesAsTools` for.
- Pack-level dotfiles no longer earn a pack an index row pointing at nothing readable; grafana's only such files are two `.gitkeep` placeholders.

### Added
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
