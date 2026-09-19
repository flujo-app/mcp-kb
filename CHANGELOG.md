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

### Added

- Discover and load verified skills in MCP Skills clients through `skills/list` and `skills/get`.

## [0.1.0] - 2026-09-18

The first release. **mcp-kb** is an MCP knowledge base: skills, prompts and agent
material collected from git, WebDAV and folders into one catalogue, served as MCP
resources — or as tools, for the clients that have none.

The image bakes nothing. One config file names the backends, the folders of
skills and prompts under them and the libraries they group into; the server reads
them at start into a cache volume, and every agent reads them from one address
space.

Everything is new, which is why there is only an *Added* section below —
*Changed* and *Fixed* are relative to a version somebody is running, and there
isn't one.

### Added

- **A config file of sources, plugins and libraries** — `CONFIG`, `/etc/mcp-kb/config.yaml` by default — where a credential is an `{env: NAME}` reference and never a value. `mcp-kb schema` prints its JSON Schema, and `config.schema.json` ships in the repository for an editor to validate against as you type.

- **A source is a backend, named once**: its URL, its credential, its `cache` and its `refresh`, written in one place however many plugins read through it.

- **A plugin is a folder of skills and prompts**, at `<source>://<path>[//<subdir>][?ref=<ref>]` — go-getter's grammar, the one kustomize and Terraform read — carrying the category, tags and keywords a catalogue entry carries. Two plugins on one repository at one ref share one clone.

- **A library is a marketplace, a list of plugins, or a query.** Point one at a repository publishing a `marketplace.json` and its entries arrive as plugins with the words their publisher gave them; add your own beside them with `plugins:`; or assemble one with a `pluginSelector` over every plugin the config knows, marketplace entries included.

- **Your own plugin against somebody else's repository.** Nothing about a marketplace's plugin is editable here — declare your own at the same address and choose its category, its tags and which of its files are served, at no second clone.

- **`file://` plugins**: a directory on this machine, needing no source entry and served in place — including a Kubernetes ConfigMap mount, which is symlinks all the way down.

- **Git sources**: `git+https://`, `git+http://` and `git+file://`, cloned bare — shallow over the network, whole for `git+file://` — and exported at the branch, tag or commit an address pins with `?ref=`, with HTTP Basic `auth` for a private remote.

- **WebDAV sources**: `webdav+https://` and `webdav+http://`, with an app password as `{env:}`; a plugin's path under one is the folder — a Nextcloud Team folder, say — copied into the cache and never downloaded again while its ETags agree.

- **`cache: live` on a WebDAV source** revalidates a file by ETag as it is read, so a file edited in Nextcloud is served on the next read with no refresh and no restart.

- **`skills`, `prompts` and `files` globs per plugin** decide what counts, falling through to the plugin's own `plugin.json` and then to the conventions when a kind is left out — and an empty list turns a kind off. A skills glob may name the directory (`skills/*`) as well as the `SKILL.md`.

- **A plugin root, and a citation that misses is tried against it.** Any non-hidden file under a plugin's root is readable at `skill://<library>/<path>`, and at a skill's own address when the skill has no such file — so a `SKILL.md` citing `shared/api.md` from its repository root resolves instead of dangling. A file the skill has always wins, and `_files.md` still lists only what `files:` names.

- **`${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_SKILL_DIR}` become addresses** in skill and prompt bodies — `skill://<library>` and `skill://<library>/<folder>/<name>` — so shared material a plugin cites is something its reader can read.

- **A cold start in milliseconds.** `index.json` in the cache records what each fetch and each plugin yielded, so a restart serves from it without touching a remote, then verifies each fingerprint in the background and rebuilds only what moved.

- **`refresh: 30s | 5m | 1h` per source** schedules that check, and `POST /reindex` forces a rebuild of everything now — re-reading every marketplace with it, so a catalogue that gained an entry gains a plugin.

- **The `skill://` address space**, on the [MCP Skills extension](https://modelcontextprotocol.io/extensions/skills/overview)'s grammar: `skill://<library>/<folder>/<skill>/SKILL.md` is the instructions, `/_manifest` is what else that skill ships, `_index.md` and `_files.md` are the indexes, and a library, a folder or a skill's own directory is not found.

- **Prompts as slash commands, in three dialects.** A Claude Code command (`argument-hint`, `$ARGUMENTS`, `$1`), a VS Code Copilot `.prompt.md` (`${input:name:hint}`) and this server's own file (declared arguments, `{{ placeholders }}`) all become the same MCP prompt, exposed as `<library>_<file stem>`. A plugin's `commands` are harvested as prompts.

- **Placeholders only a client can fill are served as written** — `${selection}`, `@path`, `#file:`, `${CLAUDE_PROJECT_DIR}` and the rest — and nothing fetched is ever executed: a `!` shell line is text.

- **A missing required argument can be a question.** On an MCP 2026-07-28 connection whose client declares elicitation, `prompts/get` asks for the argument and renders on the round that answers; every older connection keeps the -32602 naming it.

- **Four mirror tools for clients without resources or prompts** — `list_resources`, `read_resource`, `list_prompts`, `get_prompt` — revealed by `?resources=off` and `?prompts=off` (or the `X-MCP-Resources` / `X-MCP-Prompts` headers) and hidden from every client that has the real feature.

- **`?library=`, `?categories=` and `?tags=`, or `X-Skill-Library` / `X-Skill-Categories` / `X-Skill-Tags`**, narrow a client to one library, to the plugins declaring a category, or to what carries a set of tags — a ceiling the model cannot widen past, enforced on resources, prompts and every mirror tool alike. One comma rule throughout: a comma means all of, a repeated parameter means any of. A header beats the URL.

- **`?skills=full`** (or `X-Skill-Listing: full`) lists every skill rather than the indexes, for clients that sync skills to disk by scanning for `/SKILL.md`.

- **`GET /health`** reports the generation, the skill and prompt counts, and three maps — the libraries and what each serves, the plugins and what each yielded, and the fetches, each `ok`, `stale` or `failed` — with anything shipped but not served listed under `skipped`, and answers 200 whenever the process is serving, so one unreachable remote never takes a working catalogue down.

- **A scope that names nothing is refused**, with what there is instead: a library, category or tag that does not exist fails every request with -32602, so a client with a typo in its URL fails to connect and says why instead of connecting to an empty catalogue.

- **Refused credentials stop the server at boot.** A WebDAV server answering 401 or 403, or a git remote refusing authentication, while a fetch is first built exits the process with code 3 naming it, so Kubernetes restarts it until the account works; an unreachable remote still fails only its own fetch.

- **A log that says what the catalogue did**: one line when a fetch, a plugin or a library comes up, fails, goes stale, recovers or skips something, and nothing while it stays as it was. `LOG_LEVEL` sets one level and one line format for everything the process writes, and holds traffic back to `DEBUG` — a request line per MCP call, a line per WebDAV request.

- **`GET /openapi.yaml`** publishes the HTTP surface as OpenAPI 3.1, generated from the code that serves it.

- **Ships as the `kubed/mcp-kb` image** — the whole artifact: two mounts, a config file and a writable cache — and as a wheel and sdist attached to each release. The manual is the [wiki](https://github.com/kubed-io/mcp-kb/wiki).
