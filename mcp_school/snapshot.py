"""One immutable view of the whole catalogue, and how to build one.

Everything the request path touches -- the index, the pack resources, the
address space, the prompts -- hangs off a single frozen ``Snapshot``. A rebuild
constructs a new one beside the old and the server swaps it in with one
assignment, so a listing in flight is served by a consistent catalogue and no
reader ever sees a half-rebuilt one. Nothing here mutates a snapshot; there is
no method that could.

The unit of work is a *source*. ``build_source`` does the expensive part
(materialise, harvest, parse frontmatter, hash the tree) and hands back a
``SourceRecord`` -- rows, not objects, because that record is also what gets
written to ``index.json``. ``build_snapshot`` does the cheap part: turn the
records of every source into the live objects the catalogue needs. That split
is what lets a refresh rebuild one source and reuse the rest, and what lets a
cold start rebuild *nothing* and still serve.

A failed source is a record, not an exception. One unreachable directory must
not empty the catalogue of everything else, so the failure rides in
``status`` and ``/health`` reports it.

And a source that failed *after* it once succeeded is not a failed source at
all: ``stale`` keeps the last good record, rows and root intact, and hangs the
error off it. Only a source that has never been harvested has nothing better to
serve than its error.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from . import harvest
from .config import Config, Source, WebdavSource
from .index import PromptRow, SkillRow, SourceRecord, now
from .live import Revalidator, is_live
from .prompts import FilePrompt, load_prompts
from .skills import PackResources, Skill, SkillIndex, load_skills
from .sources import SourceError, fingerprint, materialise
from .uris import Catalogue

# The record states that still name a tree worth serving. A "stale" record is
# one of them: its harvest is the last good one, which is the whole point.
SERVABLE = ("ok", "stale")


@dataclass(frozen=True)
class Snapshot:
    """The catalogue as of one generation, complete and never edited again.

    ``generation`` counts rebuilds from 0 (the cold start). It is what
    ``AnnounceChanges`` compares a session against, so it must move on every
    swap and only on a swap.
    """

    generation: int
    built: str
    index: SkillIndex
    resources: PackResources
    catalogue: Catalogue
    prompts: tuple[FilePrompt, ...]
    status: dict[str, dict] = field(default_factory=dict)
    # The live sources of this generation, or None when no source is live.
    # Built here and retired here: a refresh exports a source to a new
    # directory, so a revalidator that outlived its snapshot would go on
    # writing into the tree nothing is serving.
    live: Revalidator | None = None

    def revalidate(self, path: Path) -> None:
        """Bring ``path`` level with its server, if a live source owns it."""
        if self.live is not None:
            self.live.revalidate(path)

    def stats(self, name: str) -> dict:
        """What live reads of ``name`` have done since this snapshot was built."""
        return {} if self.live is None else self.live.stats(name)


def build_source(config: Config, source: Source, cache: Path) -> SourceRecord:
    """Harvest one source from scratch: the blocking, filesystem-touching part.

    The fingerprint is taken *before* the tree is read, so a file written
    mid-harvest lands outside the recorded fingerprint and the next pass
    rebuilds. Taken after, that write would look already-accounted-for and the
    change would never be picked up.

    Everything from ``materialise`` through ``load_prompts`` runs in one try:
    a file can vanish or turn unreadable between the fingerprint walk and the
    read that follows it (a plain TOCTOU, and the whole point of this module is
    serving sources that change underneath it), and that must fail *this
    source*, not the caller. ``OSError`` is caught alongside ``SourceError`` for
    that reason -- a ``PermissionError`` or a deleted file is exactly as much
    "a failed source is a record, not an exception" as a bad URL is.
    """
    lib = config.library(source.library_name)
    try:
        root = materialise(source, cache)
        stamp = fingerprint(source, cache, root)
        tags = [*lib.tags, *source.tags]
        dirs = harvest.skill_dirs(root, source.include)
        skills = load_skills(
            dirs, pack=lib.name, source=source.name, root=root, tags=tags
        )
        files = harvest.pack_files(root, source.include, dirs)
        prompts = load_prompts(
            harvest.prompt_files(root, source.include),
            pack=lib.name,
            source=source.name,
            tags=tags,
        )
    except (SourceError, OSError) as exc:
        return _failed(source.name, lib.name, str(exc))

    return SourceRecord(
        name=source.name,
        status="ok",
        library=lib.name,
        root=str(root),
        fingerprint=stamp,
        built=now(),
        error=None,
        skills=tuple(SkillRow.from_skill(s) for s in skills),
        prompts=tuple(PromptRow.of(p.path, p) for p in prompts),
        files=tuple(files),
        skill_dirs=tuple(str(d) for d in dirs),
    )


def record_from_index(
    config: Config, source: Source, record: SourceRecord
) -> SourceRecord | None:
    """``record`` if it can still be served as it stands, else None -- rebuild it.

    Two questions only, both cheap: is the tree it names still there, and does
    it still join the library the config now says. Deliberately *not* asked: has
    the tree changed. That answer costs a walk of every source, which is what
    the cold start exists to avoid; the background pass asks it a moment later.
    """
    if record.status not in SERVABLE or record.root is None:
        return None
    if record.library != config.library(source.library_name).name:
        return None
    if not Path(record.root).is_dir():
        return None
    return record


def build_snapshot(
    config: Config, records: dict[str, SourceRecord], generation: int
) -> Snapshot:
    """Live objects from rows: the cheap half, run on every rebuild.

    Skills are rebuilt from their rows without touching disk. Prompts are
    re-parsed from the paths their rows name -- a handful of small files, and
    parsing them is far less code than serialising a rendered template and its
    arguments into the index. A prompt file that vanished since the record was
    written is logged and skipped by ``load_prompts``, so it simply stops being
    served rather than taking the source down.
    """
    live_sources = _live_sources(config, records)
    revalidator = Revalidator(live_sources) if live_sources else None
    revalidate = None if revalidator is None else revalidator.revalidate

    skills: list[Skill] = []
    resources = PackResources(revalidate)
    prompts: list[FilePrompt] = []
    status: dict[str, dict] = {}

    for source in config.sources:
        record = records.get(source.name)
        if record is None:
            continue
        if record.status not in SERVABLE or record.root is None:
            status[source.name] = {"status": "failed", "error": record.error}
            continue
        root = Path(record.root)
        skills += [row.to_skill() for row in record.skills]
        resources.add(
            record.library,
            root,
            record.files,
            [Path(d) for d in record.skill_dirs],
        )
        lib = config.library(record.library)
        loaded = load_prompts(
            [Path(row.path) for row in record.prompts],
            pack=record.library,
            source=record.name,
            tags=[*lib.tags, *source.tags],
            live=is_live(source),
        )
        prompts += loaded
        status[source.name] = {
            "status": record.status,
            "library": record.library,
            "skills": len(record.skills),
            "prompts": len(loaded),
            "files": len(record.files),
            "built": record.built,
            "fingerprint": record.fingerprint,
            # Config, so it is here rather than in the counters /health merges
            # in: an operator has to be able to see that a source is live even
            # before anything has read one of its files.
            **({"live": True} if is_live(source) else {}),
            # Only a stale record carries one, and an operator reading /health
            # needs to see why what they are being served stopped moving.
            **({"error": record.error} if record.error else {}),
        }

    index = SkillIndex(skills)
    return Snapshot(
        generation=generation,
        built=now(),
        index=index,
        resources=resources,
        catalogue=Catalogue(index, resources, revalidate),
        prompts=tuple(prompts),
        status=status,
        live=revalidator,
    )


def _live_sources(
    config: Config, records: dict[str, SourceRecord]
) -> list[tuple[WebdavSource, Path]]:
    """Every live source that has a tree, paired with the tree it is served from.

    Taken from the records rather than from the cache layout, for the same
    reason ``fingerprint`` is: an export is keyed by its version, and the one
    to revalidate into is the one *this* snapshot serves.
    """
    found = []
    for source in config.sources:
        record = records.get(source.name)
        if not is_live(source) or record is None:
            continue
        if record.status not in SERVABLE or record.root is None:
            continue
        found.append((source, Path(record.root)))
    return found


def stale(record: SourceRecord, error: str) -> SourceRecord:
    """``record`` still served, marked stale, carrying why the refresh failed.

    ``built`` is left alone on purpose: it dates the harvest being served, and
    that harvest is the one this record already held. A refresh that fails
    changes what is *known* about the source, never what is on disk for it.
    """
    return replace(record, status="stale", error=error)


def _failed(name: str, library: str, error: str) -> SourceRecord:
    return SourceRecord(
        name=name,
        status="failed",
        library=library,
        root=None,
        fingerprint={},
        built=now(),
        error=error,
        skills=(),
        prompts=(),
        files=(),
        skill_dirs=(),
    )
