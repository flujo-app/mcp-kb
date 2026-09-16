"""The skill catalogue: reading skills off disk and querying them.

Pure domain logic -- nothing here imports FastMCP or knows what MCP is.
``uris.py`` wraps this into the ``skill://`` address space that both halves of
the server project, which keeps the scoping rules in one testable place instead
of repeated in every handler.

``harvest.py`` decides *which* directories and files belong to the catalogue --
that is where the include globs, the dotfile rules and the skill-root
conventions live. This module only turns what harvest already found into
``Skill`` records and a place to read pack-level files from; it never walks a
tree on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml

from . import harvest

MAIN_FILE = "SKILL.md"


@dataclass(frozen=True)
class Skill:
    """One skill on disk."""

    name: str
    pack: str
    group: str
    description: str
    path: Path
    source: str = ""
    tags: frozenset[str] = frozenset()

    @property
    def qualified(self) -> str:
        return f"{self.pack}/{self.name}"

    def in_pack(self, selector: str) -> bool:
        """Match a selector against either the source or the group."""
        return selector in (self.pack, self.group)


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
    pack: str,
    source: str,
    root: Path,
    tags: Sequence[str] = (),
) -> list[Skill]:
    """Build ``Skill`` records for the skill directories ``harvest`` already found.

    ``pack`` is the library the skills join. ``group`` is
    ``harvest.group_of(dir, root) or pack`` -- a skill with no containing group
    (a flat source, or one sitting directly in a skill root) is in its pack's
    own group, which is the existing "flat pack" behaviour. ``tags`` is the
    library's tags plus the source's, concatenated by the caller; every skill
    additionally carries its pack, its source and the literal ``"skill"``.
    """
    base_tags = frozenset({pack, source, "skill", *tags})
    skills: list[Skill] = []
    for skill_dir in sorted(dirs):
        meta = _frontmatter(skill_dir / MAIN_FILE)
        skills.append(
            Skill(
                name=str(meta.get("name") or skill_dir.name),
                pack=pack,
                group=harvest.group_of(skill_dir, root) or pack,
                description=" ".join(str(meta.get("description", "")).split()),
                path=skill_dir,
                source=source,
                tags=base_tags,
            )
        )
    return skills


@dataclass(frozen=True)
class _Root:
    """One source's contribution to a pack: where its files live."""

    base: Path
    files: tuple[str, ...]
    skill_dirs: frozenset[Path]


class PackResources:
    """Files a pack ships that live outside every skill directory.

    The Agent Skills spec keeps a skill self-contained: references are "relative
    paths from the skill root". Some kits ignore that and factor shared material
    up to the repo root -- penpot's twelve skills point at ``shared/*`` from 190
    places. Those files are not skills and must never be listed as one, but
    without them the pack is a maze of dead links.

    So they get their own addressable space, keyed by pack. A pack can be fed by
    several sources -- ``add`` is called once per source -- so each pack holds a
    list of roots rather than one; ``files`` concatenates them in order and
    ``read`` tries them in order, returning the first hit. Membership within one
    root is decided by exclusion (anything not inside a skill directory) rather
    than by directory name, so it holds however a kit chooses to lay itself out.

    Nothing here scans a directory. ``harvest.py`` already applied the include
    globs and the dotfile rule to produce ``files``; this class only stores and
    serves what it is handed.
    """

    def __init__(self) -> None:
        self._roots: dict[str, list[_Root]] = {}

    def add(
        self,
        pack: str,
        root: Path,
        files: Sequence[str],
        skill_dirs: Sequence[Path],
    ) -> None:
        entry = _Root(
            base=root.resolve(),
            files=tuple(files),
            skill_dirs=frozenset(d.resolve() for d in skill_dirs),
        )
        self._roots.setdefault(pack, []).append(entry)

    def _in_skill(self, resolved: Path, skill_dirs: frozenset[Path]) -> bool:
        """Whether a resolved path is a skill directory or lies inside one."""
        return resolved in skill_dirs or any(
            parent in skill_dirs for parent in resolved.parents
        )

    def files(self, pack: str) -> list[str]:
        """Every pack-level file, as paths relative to whichever root holds it."""
        found: list[str] = []
        for entry in self._roots.get(pack, ()):
            found.extend(entry.files)
        return found

    def read(self, pack: str, rel: str) -> str | None:
        """Read one pack-level file, or None when it is absent or off-limits.

        Resolves before comparing so ``../`` and symlinks cannot walk out of the
        root, and refuses anything inside a skill directory -- a skill's own
        files are served as part of that skill, which applies its own scoping.
        Tries each root added for ``pack`` in order and returns the first hit.
        """
        for entry in self._roots.get(pack, ()):
            target = (entry.base / rel).resolve()
            if not target.is_relative_to(entry.base) or not target.is_file():
                continue
            if self._in_skill(target, entry.skill_dirs):
                continue
            return target.read_text(encoding="utf-8", errors="replace")
        return None


class SkillIndex:
    """A queryable catalogue that enforces the per-request scope.

    Every read goes through ``pinned``, the pack a client is restricted to.
    Centralising it here is the point: a handler that forgot to apply it would
    silently hand a scoped client somebody else's skills.
    """

    def __init__(self, skills: list[Skill]):
        self._skills = skills
        # First writer wins on the bare name, so a duplicate across packs stays
        # reachable through its qualified "<pack>/<name>" form.
        self._by_name: dict[str, Skill] = {}
        for skill in skills:
            self._by_name.setdefault(skill.name, skill)
            self._by_name[skill.qualified] = skill

    def __len__(self) -> int:
        return len(self._skills)

    @property
    def packs(self) -> list[str]:
        """Every pack in the catalogue, ignoring any request scope."""
        return sorted({s.pack for s in self._skills})

    def visible(self, pinned: str = "") -> list[Skill]:
        """The skills a client pinned to ``pinned`` may see."""
        return [s for s in self._skills if not pinned or s.in_pack(pinned)]

    def select(self, pinned: str = "", pack: str = "") -> list[Skill]:
        """Visible skills narrowed further by the caller's ``pack`` argument."""
        return [s for s in self.visible(pinned) if not pack or s.in_pack(pack)]

    def selectors(self, pinned: str = "") -> list[str]:
        """Valid ``pack`` values for this client -- packs and their groups.

        Scoped by ``pinned`` on purpose: an error message that listed every
        selector would leak the other packs' names to a pinned client.
        """
        visible = self.visible(pinned)
        return sorted({s.pack for s in visible} | {s.group for s in visible})

    def get(self, name: str, pinned: str = "") -> Skill | None:
        """Look up one skill, or None when it is absent or out of scope.

        Out-of-scope reads are indistinguishable from missing ones by design:
        knowing a skill's exact name must not be enough to confirm it exists.
        """
        found = self._by_name.get(name)
        if found is not None and pinned and not found.in_pack(pinned):
            return None
        return found
