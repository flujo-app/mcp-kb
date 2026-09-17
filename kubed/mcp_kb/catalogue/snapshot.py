"""One immutable view of the whole catalogue, and how to build one.

Everything the request path touches -- the index, the library files, the
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

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from ..config import Config, Source, WebdavSource
from ..sources import AccessRefused, SourceError, fingerprint, materialise
from ..sources.live import Revalidator, is_live
from . import harvest
from .index import PromptRow, SkillRow, SourceRecord, now
from .prompts import FilePrompt, load_prompts
from .skills import LibraryFiles, Skill, SkillIndex, load_skills, naming_problem
from .uris import INDEX, LIBRARY_FILES, SCHEME, Catalogue, _count, uri_for

log = logging.getLogger(__name__)

# The record states that still name a tree worth serving. A "stale" record is
# one of them: its harvest is the last good one, which is the whole point.
SERVABLE = ("ok", "stale")

# Names the server generates at any folder. A library file called one of these
# would be listed and then never read, since the index answers first.
RESERVED_NAMES = frozenset({INDEX, LIBRARY_FILES})


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
    resources: LibraryFiles
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


def build_source(
    config: Config, source: Source, cache: Path, *, refusal_is_fatal: bool = False
) -> SourceRecord:
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

    With ``refusal_is_fatal``, a source whose server refuses its credentials is
    the one exception, and ``AccessRefused`` escapes. The cold start asks for
    that: see ``KnowledgeBase._cold_start``.
    """
    lib = config.library(source.library_name)
    try:
        root = materialise(source, cache)
        stamp = fingerprint(source, cache, root)
        tags = [*lib.tags, *source.tags]
        dirs = harvest.skill_dirs(root, source.include)
        skills = load_skills(
            dirs, library=lib.name, source=source.name, root=root, tags=tags
        )
        files = harvest.library_files(root, source.include, dirs)
        unloadable: list[tuple[Path, str]] = []
        prompts = load_prompts(
            harvest.prompt_files(root, source.include),
            library=lib.name,
            source=source.name,
            tags=tags,
            skipped=unloadable,
        )
    except AccessRefused as exc:
        if refusal_is_fatal:
            raise
        return _failed(source.name, lib.name, str(exc))
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
        skipped=tuple(
            {"path": _relative(path, root), "reason": _unrooted(reason, root)}
            for path, reason in unloadable
        ),
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

    Within a source, what the address space has no room for is left out and
    listed under ``skipped`` in ``/health`` (see ``_admit``); everything else
    of that source is served. Sources are taken in config order, and a source
    that would serve an address or a prompt name an earlier one already serves
    is failed whole (see ``_Claims``): the earlier one keeps serving, and
    ``/health`` names the conflict.
    """
    live_sources = _live_sources(config, records)
    revalidator = Revalidator(live_sources) if live_sources else None
    revalidate = None if revalidator is None else revalidator.revalidate

    skills: list[Skill] = []
    resources = LibraryFiles(revalidate)
    prompts: list[FilePrompt] = []
    status: dict[str, dict] = {}
    claims = _Claims()

    for source in config.sources:
        record = records.get(source.name)
        if record is None:
            continue
        if record.status not in SERVABLE or record.root is None:
            status[source.name] = {"status": "failed", "error": record.error}
            continue
        root = Path(record.root)
        lib = config.library(record.library)
        vanished: list[tuple[Path, str]] = []
        loaded = load_prompts(
            [Path(row.path) for row in record.prompts],
            library=record.library,
            source=record.name,
            tags=[*lib.tags, *source.tags],
            live=is_live(source),
            skipped=vanished,
        )
        own, files, loaded, skipped = _admit(
            record, [row.to_skill() for row in record.skills], loaded
        )
        skipped = [
            *record.skipped,
            *(
                {"path": _relative(p, root), "reason": _unrooted(r, root)}
                for p, r in vanished
            ),
            *skipped,
        ]
        conflict = claims.conflict(record, own, files, loaded)
        if conflict is not None:
            status[source.name] = {"status": "failed", "error": conflict}
            continue
        claims.claim(record, own, files, loaded)
        skills += own
        resources.add(
            record.library,
            root,
            files,
            [Path(d) for d in record.skill_dirs],
            # What this source's skills carry, since these files serve them.
            tags=[record.library, record.name, "skill", *lib.tags, *source.tags],
            source=record.name,
        )
        prompts += loaded
        status[source.name] = {
            "status": record.status,
            "library": record.library,
            "skills": len(own),
            "prompts": len(loaded),
            "files": len(files),
            "built": record.built,
            "fingerprint": record.fingerprint,
            # Config, so it is here rather than in the counters /health merges
            # in: an operator has to be able to see that a source is live even
            # before anything has read one of its files.
            **({"live": True} if is_live(source) else {}),
            # Only a stale record carries one, and an operator reading /health
            # needs to see why what they are being served stopped moving.
            **({"error": record.error} if record.error else {}),
            **({"skipped": skipped} if skipped else {}),
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


def log_changes(before: dict[str, dict], after: dict[str, dict]) -> None:
    """One log line for each thing a new snapshot changed about a source.

    ``/health`` is where the state of every source can be read, and nothing reads
    it unprompted; the log is where a change of that state is *noticed*. So a
    source that comes up, fails, goes stale or recovers says so once, with its
    reason, when it happens -- at boot, where ``before`` is empty, and on every
    rebuild after -- and says nothing while it stays as it was.

    The same goes for what a source skips: a path is logged when it is first
    left out, not again on every rebuild that leaves it out still.
    """
    for name, now_ in after.items():
        was = before.get(name, {})
        state, error = now_.get("status"), now_.get("error")
        if (state, error) != (was.get("status"), was.get("error")):
            if state == "failed":
                log.error("source %s failed: %s", name, error)
            elif state == "stale":
                log.warning(
                    "source %s is stale, still serving its last harvest: %s",
                    name,
                    error,
                )
            else:
                counts = [
                    _count(now_.get(key, 0), noun)
                    for key, noun in (
                        ("skills", "skill"),
                        ("prompts", "prompt"),
                        ("files", "file"),
                    )
                ]
                log.info(
                    "source %s is serving %s, %s and %s", name, *counts
                )
        known = {row["path"] for row in was.get("skipped", ())}
        for row in now_.get("skipped", ()):
            if row["path"] not in known:
                log.warning(
                    "source %s skips %s: %s", name, row["path"], row["reason"]
                )


def _admit(
    record: SourceRecord, skills: list[Skill], prompts: list[FilePrompt]
) -> tuple[list[Skill], list[str], list[FilePrompt], list[dict[str, str]]]:
    """What of one source the address space has room for, and what it has not.

    Returns the skills, library files and prompts to serve, and a ``skipped``
    row -- the source-relative path and the reason -- for everything left out.
    A skipped thing is a defect in the source, not a failure of it: the rest
    serves, and ``/health`` says what is missing and why.

    A skill is left out when its frontmatter ``name`` breaks the Agent Skills
    naming rule or differs from its directory's name (a skill that is the
    whole source excepted) -- SEP-2640 makes the
    name the last segment of the address, so anything else mints a URI that
    does not resolve, or invents a folder -- and when a skill before it
    already has its address, as one tree reaching a skill through two skill
    roots does.

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
    served: dict[str, Skill] = {}
    for skill in skills:
        where = _relative(skill.path, root)
        problem = naming_problem(skill.name)
        # A source that is one skill has no directory of its own to match: its
        # root is wherever the cache put it, a commit hash for a git source.
        at_root = where == "."
        if problem is None and not at_root and skill.name != skill.path.name:
            problem = (
                f"name {skill.name!r} differs from its directory {skill.path.name!r}"
            )
        if problem is None and skill.address in served:
            first = _relative(served[skill.address].path, root)
            problem = f"{SCHEME}{skill.address} is already served by {first}"
        if problem is not None:
            skip(where, problem)
            continue
        served[skill.address] = skill

    roots = sorted(served, key=len, reverse=True)
    files: list[str] = []
    for rel in record.files:
        name = PurePosixPath(rel).name
        if name in RESERVED_NAMES:
            skip(rel, f"{name} is the name of an index the server generates")
            continue
        address = f"{record.library}/{rel}"
        inside = next((r for r in roots if _overlap(address, r)), None)
        if inside is not None:
            skip(rel, f"its address lies inside the skill at {SCHEME}{inside}")
            continue
        files.append(rel)

    names: dict[str, FilePrompt] = {}
    for prompt in prompts:
        where = _relative(prompt.path, root)
        if prompt.name in names:
            first = _relative(names[prompt.name].path, root)
            skip(where, f"prompt {prompt.name} is already served by {first}")
            continue
        names[prompt.name] = prompt
    return list(served.values()), files, list(names.values()), skipped


def _unrooted(text: str, root: Path) -> str:
    """``text`` with the source's root taken out of any path it quotes.

    A parse or read error names the file it failed on, absolutely; in ``/health``
    that would be the cache path ``_relative`` keeps out of every other row.
    """
    for base in (root.resolve(), root):
        text = text.replace(f"{base}/", "")
    return text


def _relative(path: Path, root: Path) -> str:
    """``path`` as the source names it: relative to its root, never absolute.

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
    """What the sources built so far serve, so a later one cannot shadow it.

    Two sources feeding one library can mint the same skill URI, the same
    library-level file URI or the same prompt name, and a reader could reach
    only one of each. Serving half of the later source would be worse than
    serving none of it -- its skills cite its own files -- so a conflict fails
    that source outright. Overlap counts, not only equality: a library file
    inside another source's skill, or a skill inside another source's skill,
    overlaps it, in either direction.

    Within one source there is nothing to claim against: ``_admit`` has
    already left out whatever that source could not serve.
    """

    skills: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)

    def conflict(
        self,
        record: SourceRecord,
        skills: list[Skill],
        files: list[str],
        prompts: list[FilePrompt],
    ) -> str | None:
        """Why ``record`` cannot be served beside the claims, or None."""
        for skill in skills:
            root = f"{SCHEME}{skill.address}"
            for claimed, owner in self.skills.items():
                if _overlap(root, claimed):
                    return _taken(uri_for(skill), owner)
            for uri, owner in self.files.items():
                if _overlap(uri, root):
                    return _taken(uri, owner)
        for rel in files:
            uri = f"{SCHEME}{record.library}/{rel}"
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
        record: SourceRecord,
        skills: list[Skill],
        files: list[str],
        prompts: list[FilePrompt],
    ) -> None:
        for skill in skills:
            self.skills.setdefault(f"{SCHEME}{skill.address}", record.name)
        for rel in files:
            self.files.setdefault(f"{SCHEME}{record.library}/{rel}", record.name)
        for prompt in prompts:
            self.prompts.setdefault(prompt.name, record.name)


def _taken(what: str, owner: str) -> str:
    return f"conflict: {what} is already served by source '{owner}'"


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
