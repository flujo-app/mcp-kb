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
    """One skill on disk."""

    name: str
    library: str
    group: str
    description: str
    path: Path
    source: str = ""
    tags: frozenset[str] = frozenset()

    @property
    def qualified(self) -> str:
        return f"{self.library}/{self.name}"

    def in_library(self, selector: str) -> bool:
        """Match a selector against either the library or the group."""
        return selector in (self.library, self.group)


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

    ``library`` is the library the skills join. ``group`` is
    ``harvest.group_of(dir, root) or library`` -- a skill with no containing
    group (a flat source, or one sitting directly in a skill root) is in its
    library's own group, which is the existing "flat library" behaviour.
    ``tags`` is the library's tags plus the source's, concatenated by the
    caller; every skill additionally carries its library, its source and the
    literal ``"skill"``.
    """
    base_tags = frozenset(t for t in (library, source, "skill", *tags) if t)
    skills: list[Skill] = []
    for skill_dir in sorted(dirs):
        meta = _frontmatter(skill_dir / harvest.MAIN_FILE)
        skills.append(
            Skill(
                name=str(meta.get("name") or skill_dir.name),
                library=library,
                group=harvest.group_of(skill_dir, root) or library,
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
    # library, so a tag scope has to be checked per root, not per library.
    tags: frozenset[str] = frozenset()

    def admits(self, tags: frozenset[str]) -> bool:
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
        entry = _Root(base=base, files=tuple(files), tags=frozenset(tags))
        self._roots.setdefault(library, []).append(entry)

    def files(self, library: str, tags: frozenset[str] = frozenset()) -> list[str]:
        """Every library-level file, as paths relative to whichever root holds it.

        ``tags`` is a request's tag scope: only roots carrying any of them count.

        A path registered by two sources of the same library is listed twice --
        known, and not reachable from the shipped config, where no library is
        fed by two sources sharing a file.
        """
        found: list[str] = []
        for entry in self._roots.get(library, ()):
            if entry.admits(tags):
                found.extend(entry.files)
        return found

    def read(
        self, library: str, rel: str, tags: frozenset[str] = frozenset()
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
            if target_rel not in entry.files or not entry.admits(tags):
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
        # First writer wins on the bare name, so a duplicate across libraries
        # stays reachable through its qualified "<library>/<name>" form.
        self._by_name: dict[str, Skill] = {}
        for skill in skills:
            self._by_name.setdefault(skill.name, skill)
            self._by_name[skill.qualified] = skill

    def __len__(self) -> int:
        return len(self._skills)

    @property
    def libraries(self) -> list[str]:
        """Every library in the catalogue, ignoring any request scope."""
        return sorted({s.library for s in self._skills})

    def visible(self, scope: Scope = EVERYTHING) -> list[Skill]:
        """The skills a client restricted to ``scope`` may see."""
        return [s for s in self._skills if _admits(scope, s)]

    def select(self, scope: Scope = EVERYTHING, library: str = "") -> list[Skill]:
        """Visible skills narrowed further by a library or group selector."""
        return [
            s for s in self.visible(scope) if not library or s.in_library(library)
        ]

    def selectors(self, scope: Scope = EVERYTHING) -> list[str]:
        """Valid selectors for this client -- libraries and their groups.

        Scoped on purpose: an error message that listed every selector would
        leak the other libraries' names to a scoped client.
        """
        visible = self.visible(scope)
        return sorted({s.library for s in visible} | {s.group for s in visible})

    def get(self, name: str, scope: Scope = EVERYTHING) -> Skill | None:
        """Look up one skill, or None when it is absent or out of scope.

        Out-of-scope reads are indistinguishable from missing ones by design:
        knowing a skill's exact name must not be enough to confirm it exists.
        """
        found = self._by_name.get(name)
        if found is not None and not _admits(scope, found):
            return None
        return found


def _admits(scope: Scope, skill: Skill) -> bool:
    return scope.admits(skill.library, skill.tags, skill.group)
