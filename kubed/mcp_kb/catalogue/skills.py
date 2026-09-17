"""The skill catalogue: reading skills off disk and querying them.

Pure domain logic -- nothing here imports FastMCP or knows what MCP is.
``uris.py`` wraps this into the ``skill://`` address space that both halves of
the server project, which keeps the scoping rules in one testable place instead
of repeated in every handler.

``harvest.py`` decides *which* directories and files belong to the catalogue --
that is where the include globs, the dotfile rules and the skill-root
conventions live. This module only turns what harvest already found into
``Skill`` records and a place to read library-level files from; it never walks a
tree on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import frontmatter
import yaml

from ..mcp.scope import EVERYTHING, Scope
from . import harvest


@dataclass(frozen=True)
class Skill:
    """One skill on disk.

    ``folder`` is where it sits below its source's skill root (see
    ``harvest.folder_of``), and ``path`` is its directory on disk. A skill is
    identified by ``address`` -- library, folder and name -- never by its name
    alone: two skills called ``testing`` in different folders are two skills.
    """

    name: str
    library: str
    folder: str
    description: str
    path: Path
    source: str = ""
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
    dirs: Sequence[Path],
    *,
    library: str,
    source: str,
    root: Path,
    tags: Sequence[str] = (),
) -> list[Skill]:
    """Build ``Skill`` records for the skill directories ``harvest`` already found.

    ``library`` is the library the skills join, and ``folder`` comes from
    ``harvest.folder_of``. ``tags`` is the library's tags plus the source's,
    concatenated by the caller; every skill additionally carries its library,
    its source and the literal ``"skill"``.
    """
    base_tags = frozenset(t for t in (library, source, "skill", *tags) if t)
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
                source=source,
                tags=base_tags,
            )
        )
    return skills


@dataclass(frozen=True)
class _Root:
    """One source's contribution to a library: where its files live."""

    base: Path
    files: tuple[str, ...]
    # The same tags this source's skills carry. Two sources can feed one
    # library, so a scope has to be checked per root, not per library.
    tags: frozenset[str] = frozenset()
    source: str = ""

    def admits(self, tags: frozenset[str], sources: frozenset[str] | None) -> bool:
        if sources is not None and self.source not in sources:
            return False
        return not tags or not tags.isdisjoint(self.tags)


class LibraryFiles:
    """Files a library ships that live outside every skill directory.

    The Agent Skills spec keeps a skill self-contained: references are "relative
    paths from the skill root". Some kits ignore that and factor shared material
    up to the repo root -- penpot's twelve skills point at ``shared/*`` from 190
    places. Those files are not skills and must never be listed as one, but
    without them the library is a maze of dead links.

    So they get their own addressable space, keyed by library. A library can be
    fed by several sources -- ``add`` is called once per source -- so each
    library holds a list of roots rather than one; ``files`` concatenates them
    in order and ``read`` tries them in order, returning the first hit.
    Membership is the list ``harvest.library_files()`` produced from the
    source's ``include.files`` globs, not "anything under the root that isn't
    inside a skill directory" -- a source that only asked for ``shared/**``
    must not let a client read ``README.md`` or ``.env`` by guessing its path.

    Nothing here scans a directory. ``harvest.py`` already applied the include
    globs, the dotfile rule and the skill-directory exclusion to produce
    ``files``; this class only stores and serves what it is handed.
    """

    def __init__(self, revalidate: Callable[[Path], None] | None = None) -> None:
        self._roots: dict[str, list[_Root]] = {}
        # A live source's files are revalidated as they are read, the same way
        # uris.py does it for a skill's own files.
        self._revalidate = revalidate

    def add(
        self,
        library: str,
        root: Path,
        files: Sequence[str],
        skill_dirs: Sequence[Path],
        tags: Sequence[str] = (),
        source: str = "",
    ) -> None:
        """Register one source's contribution to ``library``.

        ``skill_dirs`` is the defence in depth the class docstring describes:
        harvest.py already excludes a skill's own files from ``files`` before
        this is called, but a registered path that still resolves inside one of
        ``skill_dirs`` is refused rather than silently served.
        """
        base = root.resolve()
        dirs = [d.resolve() for d in skill_dirs]
        for rel in files:
            target = (base / rel).resolve()
            if any(target == d or d in target.parents for d in dirs):
                raise ValueError(f"{rel!r} lies inside a skill directory")
        entry = _Root(
            base=base, files=tuple(files), tags=frozenset(tags), source=source
        )
        self._roots.setdefault(library, []).append(entry)

    @property
    def libraries(self) -> list[str]:
        """Every library some source has added files to, ignoring any scope."""
        return sorted(
            lib for lib, roots in self._roots.items() if any(r.files for r in roots)
        )

    def files(
        self,
        library: str,
        tags: frozenset[str] = frozenset(),
        sources: frozenset[str] | None = None,
    ) -> list[str]:
        """Every library-level file, as paths relative to whichever root holds it.

        ``tags`` is a request's tag scope: only roots carrying any of them count.
        ``sources``, when given, admits only the roots those sources added.
        """
        found: list[str] = []
        for entry in self._roots.get(library, ()):
            if entry.admits(tags, sources):
                found.extend(entry.files)
        return found

    def read(
        self,
        library: str,
        rel: str,
        tags: frozenset[str] = frozenset(),
        sources: frozenset[str] | None = None,
    ) -> str | None:
        """Read one library-level file, or None when it is absent or off-limits.

        ``rel`` must be exactly one of the paths ``add()`` registered for this
        library -- the harvested list is the contract, so a file that exists on
        disk but was never harvested (an unregistered sibling, a dotfile, a
        file outside every configured ``include.files`` glob) is refused even
        though nothing here walks the directory to find that out. A registered
        path is still resolved and checked against its root before being read,
        as defence in depth against a symlink pointing outside the tree; ``add``
        already refused any path inside a skill directory, so none can be
        registered here. Tries each root added for ``library`` in order and
        returns the first hit.
        """
        target_rel = PurePosixPath(rel).as_posix()
        for entry in self._roots.get(library, ()):
            if target_rel not in entry.files or not entry.admits(tags, sources):
                continue
            target = (entry.base / rel).resolve()
            if not target.is_relative_to(entry.base) or not target.is_file():
                continue
            if self._revalidate is not None:
                self._revalidate(target)
            return target.read_text(encoding="utf-8", errors="replace")
        return None


class SkillIndex:
    """A queryable catalogue that enforces the per-request scope.

    Every read goes through a ``Scope``, the slice a client is restricted to.
    Centralising it here is the point: a handler that forgot to apply it would
    silently hand a scoped client somebody else's skills.
    """

    def __init__(self, skills: list[Skill]):
        self._skills = skills
        # First writer wins. Two sources claiming one address are refused
        # before they get here; one source reaching a skill by two paths (a
        # symlinked skill root) keeps the one found first.
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

    def selectors(self, scope: Scope = EVERYTHING) -> list[str]:
        """Valid ``library`` selectors for this client -- libraries and folders.

        Scoped on purpose: an error message that listed every selector would
        leak the other libraries' names to a scoped client.
        """
        found: set[str] = set()
        for skill in self.visible(scope):
            found.add(skill.library)
            parts = skill.folder.split("/") if skill.folder else []
            for depth in range(1, len(parts) + 1):
                found.add("/".join([skill.library, *parts[:depth]]))
        return sorted(found)

    def sources(self, scope: Scope = EVERYTHING) -> frozenset[str] | None:
        """The sources whose prompts and library-level files ``scope`` admits.

        None means any source: only a folder selector narrows by source, to the
        ones contributing a skill under that folder. A prompt or a file has no
        folder of its own, so the source is the only thing that ties it to one.
        """
        if not scope.folder:
            return None
        return frozenset(s.source for s in self.visible(scope))

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
    return scope.admits(skill.library, skill.tags, skill.folder)
