"""One immutable view of the whole catalogue, and how to build one.

Everything the request path touches -- the index, the library files, the
address space, the prompts -- hangs off a single frozen ``Snapshot``. A rebuild
constructs a new one beside the old and the server swaps it in with one
assignment, so a listing in flight is served by a consistent catalogue and no
reader ever sees a half-rebuilt one. Nothing here mutates a snapshot; there is
no method that could.

Two units of work, because two things are expensive. ``build_fetch`` does the
blocking part for a *tree* (materialise, fingerprint) and hands back a
``FetchRecord``; ``build_plugin`` does the harvest of one *plugin* out of such
a tree (find the skills, parse frontmatter, find the prompts and the files)
and hands back a ``PluginRecord`` -- rows, not objects, because both records
are also what gets written to ``index.json``. ``build_snapshot`` does the cheap
part: assign every plugin to the libraries that select it and turn its rows
into the live objects the catalogue needs. That split is what lets a refresh
rebuild one fetch and reuse the rest, and what lets a cold start rebuild
*nothing* and still serve.

A failed fetch or plugin is a record, not an exception. One unreachable
repository must not empty the catalogue of everything else, so the failure
rides in ``status`` and ``/health`` reports it.

And a fetch that failed *after* it once succeeded is not a failed fetch at all:
``stale`` keeps the last good record, root intact, and hangs the error off it.
Only a fetch that has never been materialised has nothing better to serve than
its error.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from ..config import Config
from ..plugins import Fetch, Globs, Plugin
from ..plugins.manifest import read_manifest
from ..plugins.select import select
from ..sources import AccessRefused, SourceError, fingerprint, materialise
from ..sources.live import Revalidator, is_live
from . import harvest
from .index import FetchRecord, PluginRecord, PromptRow, SkillRow, now
from .prompts import FilePrompt, load_prompts, stem
from .skills import LibraryFiles, Skill, SkillIndex, load_skills, naming_problem
from .uris import INDEX, LIBRARY_FILES, SCHEME, Catalogue, _count, uri_for

log = logging.getLogger(__name__)

# The record states that still name a tree worth serving. A "stale" record is
# one of them: its tree is the last good one, which is the whole point.
SERVABLE = ("ok", "stale")

# Names the server generates at any folder. A library file called one of these
# would be listed and then never read, since the index answers first.
RESERVED_NAMES = frozenset({INDEX, LIBRARY_FILES})

# The directories whose markdown files are Claude Code commands by convention.
# A file there is read in Claude's dialect unless the plugin says otherwise;
# detect.py cannot see paths, so the caller says it (§C1.34).
COMMAND_DIRS = ("commands", ".claude/commands")


@dataclass(frozen=True)
class Library:
    """A resolved library: which plugins it serves, in config order.

    ``plugins`` is the union the config described -- the marketplace's
    entries, then the ``plugins:`` list, then the selector's matches --
    deduplicated in that order. ``error`` is why part of that could not be
    resolved (the marketplace could not be read, a named plugin does not
    exist); the library still serves the rest.
    """

    name: str
    description: str
    plugins: tuple[str, ...]
    error: str | None = None
    skipped: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """The catalogue as of one generation, complete and never edited again.

    ``generation`` counts rebuilds from 0 (the cold start). It is what
    ``AnnounceChanges`` compares a session against, so it must move on every
    swap and only on a swap.

    ``status`` is the ``/health`` body less its counters: ``libraries``,
    ``plugins`` and ``fetches``, each keyed by name, id and key.
    """

    generation: int
    built: str
    index: SkillIndex
    resources: LibraryFiles
    catalogue: Catalogue
    prompts: tuple[FilePrompt, ...]
    status: dict[str, dict] = field(default_factory=dict)
    # The live fetches of this generation, or None when none is live. Built
    # here and retired here: a refresh exports a fetch to a new directory, so
    # a revalidator that outlived its snapshot would go on writing into the
    # tree nothing is serving.
    live: Revalidator | None = None

    def revalidate(self, path: Path) -> None:
        """Bring ``path`` level with its server, if a live fetch owns it."""
        if self.live is not None:
            self.live.revalidate(path)

    def stats(self, key: str) -> dict:
        """What live reads of the fetch ``key`` have done since this snapshot."""
        return {} if self.live is None else self.live.stats(key)


# -- the expensive half: one fetch, one plugin ----------------------------------


def build_fetch(
    fetch: Fetch, cache: Path, *, refusal_is_fatal: bool = False
) -> FetchRecord:
    """Materialise one fetch from scratch and fingerprint it: the blocking part.

    The fingerprint is taken here, *before* any plugin reads the tree, so a
    file written mid-harvest lands outside the recorded fingerprint and the
    next pass rebuilds. Taken after, that write would look already-accounted-for
    and the change would never be picked up.

    ``OSError`` is caught alongside ``SourceError``: a directory that vanishes
    between the materialise and the fingerprint is exactly as much "a failed
    fetch is a record, not an exception" as a bad URL is.

    With ``refusal_is_fatal``, a fetch whose server refuses its credentials is
    the one exception, and ``AccessRefused`` escapes. The cold start asks for
    that: see ``KnowledgeBase._cold_start``.
    """
    try:
        root = materialise(fetch, cache)
        stamp = fingerprint(fetch, cache, root)
    except AccessRefused as exc:
        if refusal_is_fatal:
            raise
        return failed_fetch(fetch.key, str(exc))
    except (SourceError, OSError) as exc:
        return failed_fetch(fetch.key, str(exc))
    return FetchRecord(
        key=fetch.key,
        status="ok",
        root=str(root),
        fingerprint=stamp,
        built=now(),
        error=None,
    )


def effective_globs(plugin: Plugin, root: Path) -> Globs:
    """What the plugin serves: its own lists, else its manifest's, else None.

    Per kind, so a plugin may name its skills and leave its commands to the
    manifest. ``None`` for a kind is ``harvest.DEFAULTS``. A manifest that does
    not parse raises: that is the plugin's failure, not a reason to guess.
    """
    manifest = read_manifest(root)
    if manifest is None:
        return plugin.globs
    own = plugin.globs
    return Globs(
        skills=own.skills if own.skills is not None else manifest.skills,
        prompts=own.prompts if own.prompts is not None else manifest.commands,
        files=own.files,
    )


def build_plugin(plugin: Plugin, root: Path, *, globs: Globs) -> PluginRecord:
    """Harvest one plugin out of its tree: the rows, and what could not be loaded.

    ``root`` is the plugin root -- the fetch root joined with the plugin's
    subdirectory -- and ``globs`` is already resolved (``effective_globs``).
    Everything from ``skill_dirs`` through ``load_prompts`` runs in one try: a
    file can vanish or turn unreadable between the fingerprint walk and the
    read that follows it (a plain TOCTOU, and the whole point of this module
    is serving trees that change underneath it), and that must fail *this
    plugin*, not the caller.
    """
    try:
        dirs = harvest.skill_dirs(root, globs)
        # Rows carry no library; the one given here is discarded by from_skill.
        skills = load_skills(dirs, library="", plugin=plugin, root=root)
        files = harvest.library_files(root, globs, dirs)
        unloadable: list[tuple[Path, str]] = []
        found = harvest.prompt_files(root, globs)
        groups: dict[str, list[Path]] = {}
        for path in found:
            groups.setdefault(_dialect(plugin, path, root), []).append(path)
        prompts = _load_prompts(plugin, groups, skipped=unloadable)
    except OSError as exc:
        return failed_plugin(plugin, unrooted(str(exc), root))

    return PluginRecord(
        id=plugin.id,
        fetch=plugin.fetch.key,
        root=str(root),
        status="ok",
        error=None,
        built=now(),
        skills=tuple(SkillRow.from_skill(s) for s in skills),
        prompts=tuple(PromptRow(path=str(p.path), dialect=p.dialect) for p in prompts),
        files=tuple(files),
        skill_dirs=tuple(str(d) for d in dirs),
        skipped=tuple(
            {"path": _relative(path, root), "reason": unrooted(reason, root)}
            for path, reason in unloadable
        ),
    )


def _load_prompts(
    plugin: Plugin,
    groups: dict[str, list[Path]],
    *,
    skipped: list[tuple[Path, str]],
    library: str = "",
    live: bool = False,
) -> list[FilePrompt]:
    """``load_prompts`` once per dialect group.

    At harvest the groups come from ``_dialect``; at snapshot time from the
    rows, whose recorded dialect is the authority -- the same file is read
    the same way on every rebuild and every restart, whatever the path rule
    would say today.
    """
    prompts: list[FilePrompt] = []
    for dialect, paths in groups.items():
        prompts += load_prompts(
            paths,
            library=library,
            plugin=plugin.id,
            tags=sorted(plugin.labels),
            category=plugin.category,
            live=live,
            dialect=dialect,
            skipped=skipped,
        )
    return sorted(prompts, key=lambda p: p.path)


def _dialect(plugin: Plugin, path: Path, root: Path) -> str:
    """The dialect a harvested file is read in: the plugin's, else by its path.

    Decided here rather than in ``detect.py`` because it is the one signal
    that lives in the path and not the file: a ``commands/`` tree is Claude's
    convention, and a description-only command in it renders the same in
    every dialect until it carries an ``argument-hint``. ``"auto"`` leaves the
    rest to ``detect``, and what it decides is what the row records.
    """
    if plugin.dialect != "auto":
        return plugin.dialect
    rel = _relative(path, root)
    if any(rel.startswith(f"{d}/") for d in COMMAND_DIRS):
        return "claude"
    return "auto"


# -- the cheap half: libraries, then the snapshot -------------------------------


def assemble_libraries(
    config: Config,
    plugins: list[Plugin],
    *,
    errors: dict[str, str] | None = None,
    skipped: dict[str, tuple[dict[str, str], ...]] | None = None,
) -> list[Library]:
    """Each library's plugins, in config order: marketplace, ``plugins:``, selector.

    ``errors`` and ``skipped`` are what reading each marketplace produced,
    keyed by library name: the caller materialised and read them, this only
    reports them beside what the library still serves. A ``plugins:`` entry
    with an ``@`` in it names a marketplace plugin that may not exist -- the
    config could not check that -- so a missing one becomes the library's
    error and the rest of the library serves.
    """
    by_id = {plugin.id: plugin for plugin in plugins}
    libraries: list[Library] = []
    for lib in config.libraries:
        problems = [errors[lib.name]] if errors and lib.name in errors else []
        ids: list[str] = []
        if lib.source:
            ids += [p.id for p in plugins if p.marketplace == lib.name]
        missing = [name for name in lib.plugins if name not in by_id]
        ids += [name for name in lib.plugins if name in by_id]
        if lib.plugin_selector is not None:
            ids += [p.id for p in select(plugins, lib.plugin_selector)]
        if missing:
            problems.append(_no_such_plugins(missing, plugins))
        libraries.append(
            Library(
                name=lib.name,
                description=lib.description,
                plugins=tuple(dict.fromkeys(ids)),
                error="; ".join(problems) or None,
                skipped=(skipped or {}).get(lib.name, ()),
            )
        )
    return libraries


def _no_such_plugins(missing: list[str], plugins: list[Plugin]) -> str:
    """Which named plugins do not exist, and what their marketplaces publish."""
    parts = []
    for name in missing:
        _, _, market = name.partition("@")
        published = sorted(p.name for p in plugins if p.marketplace == market)
        there = f"; {market} publishes: {', '.join(published)}" if published else ""
        parts.append(f"no plugin named {name!r}{there}")
    return "; ".join(parts)


def build_snapshot(
    config: Config,
    plugins: list[Plugin],
    libraries: list[Library],
    fetches: dict[str, FetchRecord],
    records: dict[str, PluginRecord],
    generation: int,
) -> Snapshot:
    """Live objects from rows: the cheap half, run on every rebuild.

    Skills are rebuilt from their rows without touching disk. Prompts are
    re-parsed from the paths their rows name, in the dialect their rows
    record -- a handful of small files, and parsing them is far less code than
    serialising a rendered template and its arguments into the index. A
    prompt file that vanished since the record was written is logged and
    skipped by ``load_prompts``, so it simply stops being served rather than
    taking the plugin down.

    Within a plugin, what the address space has no room for is left out and
    listed under the plugin's ``skipped`` in ``/health`` (see ``_admit``);
    everything else of that plugin is served. Libraries are walked in config
    order and a library's plugins in its order, and a plugin that would serve
    an address or a prompt name an earlier plugin *of that library* already
    serves fails in that library only (see ``_Claims``): the earlier one keeps
    serving, ``/health`` names the conflict under the library, and the same
    plugin serves normally in any other library.
    """
    del config  # the libraries are already resolved; kept for the call shape
    by_id = {plugin.id: plugin for plugin in plugins}
    live_fetches = _live_fetches(plugins, fetches)
    revalidator = Revalidator(live_fetches) if live_fetches else None
    revalidate = None if revalidator is None else revalidator.revalidate

    skills: list[Skill] = []
    resources = LibraryFiles(revalidate)
    prompts: list[FilePrompt] = []
    admitted = {
        pid: _admit(record)
        for pid, record in records.items()
        if record.status == "ok" and record.root is not None
    }
    # Filled by the first library that serves each plugin: the plugin's own
    # counts, and the prompt files its rows name that are no longer there.
    served: dict[str, dict[str, int]] = {}
    vanished: dict[str, list[dict[str, str]]] = {}
    status_libraries: dict[str, dict] = {}

    for lib in libraries:
        entry, own, loaded = _serve_library(
            lib, by_id, records, admitted, resources, served, vanished
        )
        skills += own
        prompts += loaded
        status_libraries[lib.name] = entry

    status_plugins = {
        plugin.id: _plugin_status(
            plugin,
            records.get(plugin.id),
            [lib.name for lib in libraries if plugin.id in lib.plugins],
            admitted.get(plugin.id),
            served.get(plugin.id),
            vanished.get(plugin.id, []),
        )
        for plugin in plugins
    }
    live_keys = {p.fetch.key for p in plugins if is_live(p.fetch)}
    status_fetches = {
        key: _fetch_status(record, key in live_keys) for key, record in fetches.items()
    }

    index = SkillIndex(skills)
    return Snapshot(
        generation=generation,
        built=now(),
        index=index,
        resources=resources,
        catalogue=Catalogue(index, resources, revalidate),
        prompts=tuple(prompts),
        status={
            "libraries": status_libraries,
            "plugins": status_plugins,
            "fetches": status_fetches,
        },
        live=revalidator,
    )


def _serve_library(
    lib: Library,
    by_id: dict[str, Plugin],
    records: dict[str, PluginRecord],
    admitted: dict[str, _Admitted],
    resources: LibraryFiles,
    served: dict[str, dict[str, int]],
    vanished: dict[str, list[dict[str, str]]],
) -> tuple[dict, list[Skill], list[FilePrompt]]:
    """One library's plugins in order: its ``/health`` entry, skills and prompts.

    Registers the library's files on ``resources`` as it goes. ``served`` and
    ``vanished`` are written for a plugin the first time any library serves
    it, since both are the plugin's own and the same in every library.
    """
    claims = _Claims()
    entry: dict = {
        "description": lib.description,
        "plugins": list(lib.plugins),
        "skills": 0,
        "prompts": 0,
        "files": 0,
    }
    conflicts: dict[str, str] = {}
    skills: list[Skill] = []
    prompts: list[FilePrompt] = []
    for pid in lib.plugins:
        plugin = by_id[pid]
        if pid not in admitted:
            continue
        root = Path(records[pid].root or "")
        own = [
            row.to_skill(
                library=lib.name,
                plugin=pid,
                root=root,
                category=plugin.category,
                tags=plugin.labels,
            )
            for row in admitted[pid].skills
        ]
        groups: dict[str, list[Path]] = {}
        for row in admitted[pid].prompts:
            groups.setdefault(row.dialect, []).append(Path(row.path))
        gone: list[tuple[Path, str]] = []
        loaded = _load_prompts(
            plugin, groups, skipped=gone, library=lib.name, live=is_live(plugin.fetch)
        )
        vanished.setdefault(
            pid,
            [
                {"path": _relative(p, root), "reason": unrooted(r, root)}
                for p, r in gone
            ],
        )
        files = admitted[pid].files
        conflict = claims.conflict(lib.name, own, files, loaded)
        if conflict is not None:
            conflicts[pid] = conflict
            continue
        claims.claim(lib.name, pid, own, files, loaded)
        skills += own
        resources.add(
            lib.name,
            root,
            files,
            [Path(d) for d in records[pid].skill_dirs],
            plugin=plugin,
        )
        prompts += loaded
        counts = {"skills": len(own), "prompts": len(loaded), "files": len(files)}
        served.setdefault(pid, counts)
        for key, count in counts.items():
            entry[key] += count
    if lib.error:
        entry["error"] = lib.error
    if lib.skipped:
        entry["skipped"] = list(lib.skipped)
    if conflicts:
        entry["conflicts"] = conflicts
    return entry, skills, prompts


def _plugin_status(
    plugin: Plugin,
    record: PluginRecord | None,
    libraries: list[str],
    admitted: _Admitted | None,
    served: dict[str, int] | None,
    vanished: list[dict[str, str]],
) -> dict:
    """One plugin's entry in ``/health``: what it is, and what it yielded."""
    entry: dict = {
        "status": record.status if record is not None else "failed",
        "fetch": plugin.fetch.key,
        "root": plugin.subdir,
        "category": plugin.category,
        "tags": sorted(plugin.tags),
        "keywords": sorted(plugin.keywords),
        **({"version": plugin.version} if plugin.version else {}),
        "libraries": libraries,
    }
    if record is None or admitted is None:
        # A record that is not ok carries why; one that never got a root has
        # nothing to say, and the key is typed as a string.
        entry["error"] = (record.error if record is not None else None) or (
            "not harvested"
        )
        return entry
    # The plugin's own counts, library-independent: what a library gets from
    # it. Served once by some library, its prompts are the ones that parsed;
    # served by none, they are the rows the harvest recorded.
    counts = served or {
        "skills": len(admitted.skills),
        "prompts": len(admitted.prompts),
        "files": len(admitted.files),
    }
    entry.update(counts)
    entry["built"] = record.built
    skipped = [*record.skipped, *admitted.skipped, *vanished]
    if skipped:
        entry["skipped"] = skipped
    return entry


def _fetch_status(record: FetchRecord, live: bool) -> dict:
    return {
        "status": record.status,
        "built": record.built,
        "fingerprint": record.fingerprint,
        # Config, so it is here rather than in the counters /health merges
        # in: an operator has to be able to see that a fetch is live even
        # before anything has read one of its files.
        **({"live": True} if live else {}),
        # A failed record carries why it failed; a stale one why what is being
        # served stopped moving.
        **({"error": record.error} if record.error else {}),
    }


def log_changes(before: dict[str, dict], after: dict[str, dict]) -> None:
    """One log line for each thing a new snapshot changed.

    ``/health`` is where the state of every fetch, plugin and library can be
    read, and nothing reads it unprompted; the log is where a change of that
    state is *noticed*. So a fetch that fails, goes stale or recovers says so
    once, with its reason, when it happens -- at boot, where ``before`` is
    empty, and on every rebuild after -- and says nothing while it stays as it
    was. A library says what it serves whenever that count or its error
    changes; a plugin says once what it skips and once why it failed.
    """
    for key, now_ in after.get("fetches", {}).items():
        was = before.get("fetches", {}).get(key, {})
        state, error = now_.get("status"), now_.get("error")
        if (state, error) == (was.get("status"), was.get("error")):
            continue
        if state == "failed":
            log.error("fetch %s failed: %s", key, error)
        elif state == "stale":
            log.warning(
                "fetch %s is stale, still serving its last tree: %s", key, error
            )
        else:
            log.info("fetch %s is ok", key)

    for pid, now_ in after.get("plugins", {}).items():
        was = before.get("plugins", {}).get(pid, {})
        if now_.get("status") == "failed" and (
            (now_.get("status"), now_.get("error"))
            != (was.get("status"), was.get("error"))
        ):
            log.error("plugin %s failed: %s", pid, now_.get("error"))
        known = {row["path"] for row in was.get("skipped", ())}
        for row in now_.get("skipped", ()):
            if row["path"] not in known:
                log.warning("plugin %s skips %s: %s", pid, row["path"], row["reason"])

    for name, now_ in after.get("libraries", {}).items():
        was = before.get("libraries", {}).get(name, {})
        watched = ("skills", "prompts", "files", "error", "conflicts")
        if all(now_.get(k) == was.get(k) for k in watched):
            continue
        counts = [
            _count(now_.get(key, 0), noun)
            for key, noun in (
                ("skills", "skill"),
                ("prompts", "prompt"),
                ("files", "file"),
            )
        ]
        log.info("library %s is serving %s, %s and %s", name, *counts)
        if now_.get("error") and now_.get("error") != was.get("error"):
            log.warning("library %s: %s", name, now_["error"])
        for pid, message in now_.get("conflicts", {}).items():
            if message != was.get("conflicts", {}).get(pid):
                log.error("library %s: plugin %s: %s", name, pid, message)


@dataclass(frozen=True)
class _Admitted:
    """What of one plugin the address space has room for, library-independent."""

    skills: list[SkillRow]
    files: list[str]
    prompts: list[PromptRow]
    skipped: list[dict[str, str]]


def _admit(record: PluginRecord) -> _Admitted:
    """What of one plugin the address space has room for, and what it has not.

    Returns the skill rows, library files and prompt rows to serve, and a
    ``skipped`` row -- the plugin-relative path and the reason -- for
    everything left out. A skipped thing is a defect in the plugin, not a
    failure of it: the rest serves, and ``/health`` says what is missing and
    why. Decided per plugin, not per library: the address a skill takes below
    a library is the same in every library that serves the plugin.

    A skill is left out when its frontmatter ``name`` breaks the Agent Skills
    naming rule or differs from its directory's name (a skill that is the whole
    plugin excepted) -- SEP-2640 makes the name the last segment of the
    address, so anything else mints a URI that does not resolve, or invents a
    folder -- and when a skill before it already has its address, as one tree
    reaching a skill through two skill roots does.

    A library file is left out when its address is a served skill's or lies
    inside one -- the skill answers that URI, so the file could be listed and
    never read, or read only under a scope that hides the skill -- and when it
    is named like an index the server generates.

    A prompt is left out when a prompt before it has its name, as
    ``prompts/debug.md`` and ``prompts/sub/debug.md`` both would.
    """
    skipped: list[dict[str, str]] = []

    def skip(path: str, reason: str) -> None:
        # Not logged here: a snapshot is rebuilt on every refresh, and this
        # would repeat the same line each time. ``log_changes`` says it once.
        skipped.append({"path": path, "reason": reason})

    root = Path(record.root or "")
    served: dict[str, SkillRow] = {}
    for row in record.skills:
        path = Path(row.path)
        where = _relative(path, root)
        problem = naming_problem(row.name)
        # A plugin that is one skill has no directory of its own to match: its
        # root is wherever the cache put it, a commit hash for a git fetch.
        at_root = where == "."
        if problem is None and not at_root and row.name != path.name:
            problem = f"name {row.name!r} differs from its directory {path.name!r}"
        address = "/".join(p for p in (row.folder, row.name) if p)
        if problem is None and address in served:
            first = _relative(Path(served[address].path), root)
            problem = f"{address} is already served by {first}"
        if problem is not None:
            skip(where, problem)
            continue
        served[address] = row

    roots = sorted(served, key=len, reverse=True)
    files: list[str] = []
    for rel in record.files:
        name = PurePosixPath(rel).name
        if name in RESERVED_NAMES:
            skip(rel, f"{name} is the name of an index the server generates")
            continue
        inside = next((r for r in roots if _overlap(rel, r)), None)
        if inside is not None:
            skip(rel, f"its address lies inside the skill at {inside}")
            continue
        files.append(rel)

    names: dict[str, PromptRow] = {}
    for row in record.prompts:
        name = stem(Path(row.path))
        if name in names:
            first = _relative(Path(names[name].path), root)
            where = _relative(Path(row.path), root)
            skip(where, f"prompt {name} is already served by {first}")
            continue
        names[name] = row
    return _Admitted(list(served.values()), files, list(names.values()), skipped)


def unrooted(text: str, root: Path) -> str:
    """``text`` with the plugin's root taken out of any path it quotes.

    A parse or read error names the file it failed on, absolutely; in ``/health``
    that would be the cache path ``_relative`` keeps out of every other row.
    Public because the server reads a marketplace off the same kind of root
    and publishes what went wrong the same way.
    """
    for base in (root.resolve(), root):
        text = text.replace(f"{base}/", "")
    return text


def _relative(path: Path, root: Path) -> str:
    """``path`` as the plugin names it: relative to its root, never absolute.

    ``/health`` is unauthenticated, so a cache path does not belong in it. A
    harvest resolves the root it walks, so either spelling may be the prefix.
    """
    for base in (root, root.resolve()):
        if path.is_relative_to(base):
            return path.relative_to(base).as_posix()
    return path.name


def _within(address: str, root: str) -> bool:
    """Whether ``address`` is ``root`` or lies under it, segment-wise."""
    return address == root or address.startswith(f"{root}/")


def _overlap(a: str, b: str) -> bool:
    """Whether either address is the other or lies under it.

    Both directions matter: a file inside a skill shadows the skill's file, and a
    skill under a file's address turns that address into a directory that must
    serve nothing.
    """
    return _within(a, b) or _within(b, a)


@dataclass
class _Claims:
    """What one library's plugins serve so far, so a later one cannot shadow it.

    Two plugins feeding one library can mint the same skill URI, the same
    library-level file URI or the same prompt name, and a reader could reach
    only one of each. Serving half of the later plugin would be worse than
    serving none of it -- its skills cite its own files -- so a conflict fails
    that plugin in this library. Overlap counts, not only equality: a library
    file inside another plugin's skill, or a skill inside another plugin's
    skill, overlaps it, in either direction.

    Per library, because addresses are: the same two plugins in two libraries
    are two independent questions, and a plugin failed here serves normally
    there.
    """

    skills: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)

    def conflict(
        self,
        library: str,
        skills: list[Skill],
        files: list[str],
        prompts: list[FilePrompt],
    ) -> str | None:
        """Why these cannot be served beside the claims, or None."""
        for skill in skills:
            root = f"{SCHEME}{skill.address}"
            for claimed, owner in self.skills.items():
                if _overlap(root, claimed):
                    return _taken(uri_for(skill), owner)
            for uri, owner in self.files.items():
                if _overlap(uri, root):
                    return _taken(uri, owner)
        for rel in files:
            uri = f"{SCHEME}{library}/{rel}"
            claims = (*self.files.items(), *self.skills.items())
            owner = next((o for c, o in claims if _overlap(uri, c)), None)
            if owner is not None:
                return _taken(uri, owner)
        for prompt in prompts:
            owner = self.prompts.get(prompt.name)
            if owner is not None:
                return _taken(f"prompt {prompt.name}", owner)
        return None

    def claim(
        self,
        library: str,
        plugin: str,
        skills: list[Skill],
        files: list[str],
        prompts: list[FilePrompt],
    ) -> None:
        for skill in skills:
            self.skills.setdefault(f"{SCHEME}{skill.address}", plugin)
        for rel in files:
            self.files.setdefault(f"{SCHEME}{library}/{rel}", plugin)
        for prompt in prompts:
            self.prompts.setdefault(prompt.name, plugin)


def _taken(what: str, owner: str) -> str:
    return f"conflict: {what} is already served by plugin '{owner}'"


def _live_fetches(
    plugins: Iterable[Plugin], fetches: dict[str, FetchRecord]
) -> list[tuple[Fetch, Path]]:
    """Every live fetch that has a tree, paired with the tree it is served from.

    Taken from the records rather than from the cache layout, for the same
    reason ``fingerprint`` is: an export is keyed by its version, and the one
    to revalidate into is the one *this* snapshot serves.
    """
    found: dict[str, tuple[Fetch, Path]] = {}
    for plugin in plugins:
        fetch = plugin.fetch
        record = fetches.get(fetch.key)
        if not is_live(fetch) or record is None or fetch.key in found:
            continue
        if record.status not in SERVABLE or record.root is None:
            continue
        found[fetch.key] = (fetch, Path(record.root))
    return list(found.values())


def stale(record: FetchRecord, error: str) -> FetchRecord:
    """``record`` still served, marked stale, carrying why the refresh failed.

    ``built`` is left alone on purpose: it dates the tree being served, and
    that tree is the one this record already held. A refresh that fails
    changes what is *known* about the fetch, never what is on disk for it.
    """
    return replace(record, status="stale", error=error)


def failed_fetch(key: str, error: str) -> FetchRecord:
    return FetchRecord(
        key=key, status="failed", root=None, fingerprint={}, built=now(), error=error
    )


def failed_plugin(plugin: Plugin, error: str) -> PluginRecord:
    return PluginRecord(
        id=plugin.id,
        fetch=plugin.fetch.key,
        root=None,
        status="failed",
        error=error,
        built=now(),
        skills=(),
        prompts=(),
        files=(),
        skill_dirs=(),
    )
