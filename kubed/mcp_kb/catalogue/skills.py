"""The skill catalogue: reading skills off disk and querying them.

Pure domain logic -- nothing here imports FastMCP or knows what MCP is.
``uris.py`` wraps this into the ``skill://`` address space that both halves of
the server project, which keeps the scoping rules in one testable place instead
of repeated in every handler.

``harvest.py`` decides *which* directories and files belong to the catalogue --
that is where the globs, the dotfile rules and the skill-root conventions live.
This module only turns what harvest already found into ``Skill`` records and a
place to read library-level files from; it never walks a tree on its own.

What a scope matches on is the *plugin's*: its category and its labels (tags
and keywords). A skill carries them so that a scope can be decided per skill
without reaching back to the plugin, and a library's files carry the plugin
itself, because a file has no row of its own to copy them onto.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import frontmatter
import yaml

from ..mcp.scope import EVERYTHING, Scope
from . import harvest

if TYPE_CHECKING:
    from ..plugins import Plugin

# The Agent Skills naming rule: lowercase letters, digits and single hyphens,
# neither first nor last. It is also what keeps a name one URI segment.
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
NAME_MAX = 64


def naming_problem(name: str) -> str | None:
    """Why ``name`` breaks the Agent Skills naming rule, or None when it keeps it."""
    if len(name) <= NAME_MAX and NAME.fullmatch(name):
        return None
    return (
        f"name {name!r} breaks the Agent Skills naming rule: 1-{NAME_MAX}"
        " lowercase letters, digits and single hyphens, not first or last"
    )


@dataclass(frozen=True)
class Skill:
    """One skill on disk, as one library serves it.

    ``folder`` is where it sits below its plugin's skill root (see
    ``harvest.folder_of``), ``path`` is its directory on disk and ``root`` the
    plugin root it was harvested from. A skill is identified by ``address`` --
    library, folder and name -- never by its name alone: two skills called
    ``testing`` in different folders are two skills, and one plugin served by
    two libraries is two skills at two addresses.

    ``category`` and ``tags`` are the plugin's, copied here so a scope is a
    question the skill answers by itself.
    """

    name: str
    library: str
    folder: str
    description: str
    path: Path
    plugin: str
    root: Path
    category: str | None = None
    tags: frozenset[str] = frozenset()

    @property
    def address(self) -> str:
        """``<library>/<folder>/<name>``, without an empty folder."""
        return "/".join(p for p in (self.library, self.folder, self.name) if p)


def _frontmatter(skill_md: Path) -> dict:
    """Parse a SKILL.md YAML frontmatter block, tolerating a malformed one."""
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    try:
        meta = frontmatter.loads(text).metadata
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def load_skills(
    dirs: Sequence[Path], *, library: str, plugin: Plugin, root: Path
) -> list[Skill]:
    """Build ``Skill`` records for the skill directories ``harvest`` already found.

    ``library`` is the library the skills join, ``root`` the plugin root the
    folders are taken below, and every skill carries the plugin's id, category
    and labels -- nothing implicit: a library or a plugin name is not a tag.
    """
    skills: list[Skill] = []
    for skill_dir in sorted(dirs):
        meta = _frontmatter(skill_dir / harvest.MAIN_FILE)
        skills.append(
            Skill(
                name=str(meta.get("name") or skill_dir.name),
                library=library,
                folder=harvest.folder_of(skill_dir, root),
                description=" ".join(str(meta.get("description", "")).split()),
                path=skill_dir,
                plugin=plugin.id,
                root=root,
                category=plugin.category,
                tags=plugin.labels,
            )
        )
    return skills


@dataclass(frozen=True)
class _Root:
    """One plugin's contribution to a library: where its files live."""

    base: Path
    files: tuple[str, ...]
    # The plugin whose files these are: what a scope's category and tags are
    # checked against, since a file has no row of its own to carry them.
    plugin: Plugin
    # The plugin's skill directories, resolved: what the root-wide fallback
    # must not reach, since those paths are the skills' own addresses.
    skills: tuple[Path, ...] = ()

    def admits(self, scope: Scope) -> bool:
        return scope.admits_labels(self.plugin.category, self.plugin.labels)

    def holds(self, rel: str, excluded: Iterable[Path] = ()) -> Path | None:
        """``rel`` as a file inside this root that a fallback may serve, or None.

        ``harvest.readable`` decides containment; on top of it, a path inside
        one of the plugin's skills is refused because that file already has an
        address -- the skill's -- whether or not the skill is one the snapshot
        went on to serve. ``excluded`` adds the other plugins' skills, for a
        caller that knows the whole library (see ``LibraryFiles._skill_dirs``).
        """
        target = harvest.readable(self.base, rel)
        if target is None:
            return None
        if _inside(target, (*self.skills, *excluded)):
            return None
        return target

    def shadowed(self, rel: str, excluded: Iterable[Path] = ()) -> bool:
        """Whether ``rel`` under this root belongs to a skill rather than here.

        The same rule ``holds`` applies, on the path alone: a listing asks
        whose address this is, not whether the bytes are there this second --
        a live tree's file may come and go between a listing and a read, and
        that is not this question.
        """
        return _inside((self.base / rel).resolve(), (*self.skills, *excluded))


def _inside(target: Path, dirs: Iterable[Path]) -> bool:
    """Whether ``target`` is one of ``dirs`` or sits under one."""
    return any(target == d or d in target.parents for d in dirs)


class LibraryFiles:
    """Files a library ships that live outside every skill directory.

    The Agent Skills spec keeps a skill self-contained: references are "relative
    paths from the skill root". Some kits ignore that and factor shared material
    up to the repo root -- penpot's twelve skills point at ``shared/*`` from 190
    places. Those files are not skills and must never be listed as one, but
    without them the library is a maze of dead links.

    So they get their own addressable space, keyed by library. A library is fed
    by several plugins -- ``add`` is called once per plugin -- so each library
    holds a list of roots rather than one; ``files`` concatenates them in order
    and ``read`` tries them in order, returning the first hit. Membership is
    the list ``harvest.library_files()`` produced from the plugin's ``files``
    globs, not "anything under the root that isn't inside a skill directory"
    -- a plugin that only asked for ``shared/**`` must not let a client read
    ``README.md`` or ``.env`` by guessing its path.

    Nothing here scans a directory. ``harvest.py`` already applied the globs,
    the dotfile rule and the skill-directory exclusion to produce ``files``;
    this class only stores and serves what it is handed -- with one correction
    it is the only place that can make. Harvest and ``add`` both see one plugin
    at a time, so a helper plugin globbing ``**/*`` over a root it shares with a
    kit arrives holding the kit's skill files. Only a library knows all its
    roots, so every method here excludes ``_skill_dirs(library)``: the listing
    and the read agree, and neither answers for a skill.

    ``read_any`` is the one read that is not the harvested list: the plugin
    root as a fallback, for the paths a kit's own text points at. It lists
    nothing -- see its docstring.
    """

    def __init__(self, revalidate: Callable[[Path], None] | None = None) -> None:
        self._roots: dict[str, list[_Root]] = {}
        # A live fetch's files are revalidated as they are read, the same way
        # uris.py does it for a skill's own files.
        self._revalidate = revalidate

    def add(
        self,
        library: str,
        root: Path,
        files: Sequence[str],
        skill_dirs: Sequence[Path],
        *,
        plugin: Plugin,
    ) -> None:
        """Register one plugin's contribution to ``library``.

        ``skill_dirs`` is the defence in depth the class docstring describes:
        harvest.py already excludes a skill's own files from ``files`` before
        this is called, but a registered path that still resolves inside one of
        ``skill_dirs`` is refused rather than silently served.
        """
        base = root.resolve()
        dirs = [d.resolve() for d in skill_dirs]
        for rel in files:
            if _inside((base / rel).resolve(), dirs):
                raise ValueError(f"{rel!r} lies inside a skill directory")
        entry = _Root(base=base, files=tuple(files), plugin=plugin, skills=tuple(dirs))
        self._roots.setdefault(library, []).append(entry)

    @property
    def libraries(self) -> list[str]:
        """Every library some plugin has added files to, ignoring any scope."""
        return sorted(
            lib for lib, roots in self._roots.items() if any(r.files for r in roots)
        )

    def files(self, library: str, scope: Scope = EVERYTHING) -> list[str]:
        """Every library-level file, as paths relative to whichever root holds it.

        ``scope``'s category and tags decide which plugins' roots count; its
        library is the caller's to have checked, since ``library`` is the one
        being asked about.

        A path inside any skill directory of the library is left out, the same
        exclusion ``read`` and the fallback apply: ``add`` could only refuse
        the registering plugin's own skills, and a helper plugin globbing
        ``**/*`` over a root it shares with a kit harvests the kit's skill
        files. Listing them would advertise an address this does not answer.
        """
        excluded = self._skill_dirs(library)
        found: list[str] = []
        for entry in self._roots.get(library, ()):
            if not entry.admits(scope):
                continue
            found.extend(
                rel for rel in entry.files if not entry.shadowed(rel, excluded)
            )
        return found

    def read(self, library: str, rel: str, scope: Scope = EVERYTHING) -> str | None:
        """Read one library-level file, or None when it is absent or off-limits.

        ``rel`` must be exactly one of the paths ``add()`` registered for this
        library -- the harvested list is the contract, so a file that exists on
        disk but was never harvested (an unregistered sibling, a dotfile, a
        file outside every configured ``files`` glob) is refused even though
        nothing here walks the directory to find that out. A registered path
        is still resolved and checked against its root before being read, as
        defence in depth against a symlink pointing outside the tree, and
        against every skill directory of the library -- ``add`` could only
        refuse the registering plugin's own, and ``files`` leaves out exactly
        the same paths, so nothing listed here cannot be read and nothing
        readable belongs to a skill a scope hides. Tries each root added for
        ``library`` in order and returns the first hit.
        """
        target_rel = PurePosixPath(rel).as_posix()
        excluded = self._skill_dirs(library)
        for entry in self._roots.get(library, ()):
            if target_rel not in entry.files or not entry.admits(scope):
                continue
            target = entry.holds(rel, excluded)
            if target is not None:
                return self._serve(target)
        return None

    def read_any(self, library: str, rel: str, scope: Scope = EVERYTHING) -> str | None:
        """Read anything else under a plugin root of ``library``, or None.

        The fallback for the addresses a kit's own text points at: a skill that
        factors material up out of itself cites it from the plugin root, which
        is what ``${CLAUDE_PLUGIN_ROOT}`` names, and neither the citation nor
        the placeholder knows anything about a ``files:`` glob. So the plugin
        root -- not the harvested list -- is the ceiling here, and everything
        the harvest leaves out of a *listing* stays out of one: what this reads
        is unlisted, and ``files()`` is unchanged.

        Hidden paths are refused as everywhere else, and ``_Root.holds`` decides
        containment, against every skill directory of the library. Roots are
        tried in the order the library declares its plugins, so a file two
        plugins ship is the first one's, as it is for a harvested file.
        """
        if harvest.hidden(Path(rel)):
            return None
        excluded = self._skill_dirs(library)
        for entry in self._roots.get(library, ()):
            if not entry.admits(scope):
                continue
            target = entry.holds(rel, excluded)
            if target is not None:
                return self._serve(target)
        return None

    def read_under(self, library: str, root: Path, rel: str) -> str | None:
        """Read ``rel`` under one plugin root of ``library``, or None.

        What ``read_any`` does for every root, for the one root a caller
        already has: ``uris.py``'s skill-level fallback, where the root is the
        skill's own plugin's and the scope was decided by that skill being
        visible at all. Same rule, so a path inside any skill directory of the
        library is refused here too -- reaching one through a sibling's address
        would serve one file at two addresses.
        """
        base = root.resolve()
        excluded = self._skill_dirs(library)
        for entry in self._roots.get(library, ()):
            if entry.base != base:
                continue
            target = entry.holds(rel, excluded)
            if target is not None:
                return self._serve(target)
        return None

    def _skill_dirs(self, library: str) -> tuple[Path, ...]:
        """Every skill directory of every plugin registered for ``library``.

        What ``files``, ``read``, ``read_any`` and ``read_under`` all exclude:
        the ceiling is the library's, not the answering plugin's. Two plugins
        on one root -- the shape ``penpot-shared`` has, and the one an operator
        writes to add files beside somebody else's kit -- would otherwise let a
        scope that admits only the file-shipping plugin list and read the
        other's skill files at the library base, at an address the skill's own
        scope refuses. One URI, one answer, under every scope.

        Computed per call rather than at ``add``: a library's roots arrive one
        plugin at a time, so only a reader sees them all.
        """
        return tuple(d for entry in self._roots.get(library, ()) for d in entry.skills)

    def _serve(self, target: Path) -> str:
        # A live fetch's file may have moved since it was copied, and a read is
        # the only thing that asks.
        if self._revalidate is not None:
            self._revalidate(target)
        return target.read_text(encoding="utf-8", errors="replace")


class SkillIndex:
    """A queryable catalogue that enforces the per-request scope.

    Every read goes through a ``Scope``, the slice a client is restricted to.
    Centralising it here is the point: a handler that forgot to apply it would
    silently hand a scoped client somebody else's skills.
    """

    def __init__(self, skills: list[Skill]):
        self._skills = skills
        # First writer wins, though no two skills here share an address: the
        # snapshot fails a second plugin that would, and skips a second skill
        # of one plugin.
        self._by_address: dict[str, Skill] = {}
        for skill in skills:
            self._by_address.setdefault(skill.address, skill)

    def __len__(self) -> int:
        return len(self._skills)

    @property
    def libraries(self) -> list[str]:
        """Every library in the catalogue, ignoring any request scope."""
        return sorted({s.library for s in self._skills})

    def visible(self, scope: Scope = EVERYTHING) -> list[Skill]:
        """The skills a client restricted to ``scope`` may see."""
        return [s for s in self._skills if _admits(scope, s)]

    def get(self, address: str, scope: Scope = EVERYTHING) -> Skill | None:
        """Look up one skill by its address, or None when absent or out of scope.

        Out-of-scope reads are indistinguishable from missing ones by design:
        knowing a skill's exact address must not be enough to confirm it exists.
        """
        found = self._by_address.get(address)
        if found is not None and not _admits(scope, found):
            return None
        return found


def _admits(scope: Scope, skill: Skill) -> bool:
    return scope.admits(skill.library, skill.category, skill.tags)
