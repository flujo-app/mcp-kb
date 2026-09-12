"""The skill catalogue: reading skills off disk and querying them.

Pure domain logic -- nothing here imports FastMCP or knows what MCP is.
``uris.py`` wraps this into the ``skill://`` address space that both halves of
the server project, which keeps the scoping rules in one testable place instead
of repeated in every handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

MAIN_FILE = "SKILL.md"


@dataclass(frozen=True)
class Skill:
    """One skill on disk."""

    name: str
    pack: str
    group: str
    description: str
    path: Path

    @property
    def qualified(self) -> str:
        return f"{self.pack}/{self.name}"

    def in_pack(self, selector: str) -> bool:
        """Match a selector against either the source or the group."""
        return selector in (self.pack, self.group)


def _frontmatter(skill_md: Path) -> dict:
    """Parse a SKILL.md YAML frontmatter block, tolerating a malformed one."""
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    _, _, rest = text.partition("---")
    block, sep, _ = rest.partition("\n---")
    if not sep:
        return {}
    try:
        return yaml.safe_load(block) or {}
    except yaml.YAMLError:
        return {}


def load_skills(base: Path, packs: list[str] | None = None) -> list[Skill]:
    """Read every skill under ``base``.

    ``pack`` is the top-level directory under ``base`` -- one skill source, one
    pack. ``group`` is the directory that immediately contains the skill, which
    for a nested source (grafana) is a meaningful sub-family and for a flat one
    (n8n) is just the pack again. Both are selectable, so an agent can narrow to
    a whole source or to one family within it.

    ``packs`` hard-scopes the server to a subset, which is how a single image
    serves a narrower catalogue without a separate build.
    """
    if not base.is_dir():
        return []

    skills: list[Skill] = []
    for skill_md in sorted(base.rglob(MAIN_FILE)):
        rel = skill_md.relative_to(base)
        if len(rel.parts) < 2:
            continue
        pack = rel.parts[0]
        if packs and pack not in packs:
            continue
        meta = _frontmatter(skill_md)
        skills.append(
            Skill(
                name=str(meta.get("name") or skill_md.parent.name),
                pack=pack,
                group=rel.parts[-3],
                description=" ".join(str(meta.get("description", "")).split()),
                path=skill_md.parent,
            )
        )
    return skills


class PackResources:
    """Files a pack ships that live outside every skill directory.

    The Agent Skills spec keeps a skill self-contained: references are "relative
    paths from the skill root". Some kits ignore that and factor shared material
    up to the repo root -- penpot's twelve skills point at ``shared/*`` from 190
    places. Those files are not skills and must never be listed as one, but
    without them the pack is a maze of dead links.

    So they get their own addressable space, keyed by pack. Membership is
    decided by exclusion (anything not inside a skill directory) rather than by
    directory name, so it holds however a kit chooses to lay itself out.
    """

    def __init__(self, base: Path, skills: list[Skill]):
        self._base = base
        self._skill_dirs = {s.path.resolve() for s in skills}
        # Derived from the skills actually loaded, so SKILL_PACKS scopes this
        # too. Reading it off disk instead would let a scoped instance serve a
        # pack whose skills it deliberately does not index.
        self._packs = {s.pack for s in skills}

    def _is_resource(self, path: Path) -> bool:
        # Dotfiles are never material a skill references -- grafana's only
        # pack-level files are two .gitkeep placeholders, and listing them wins
        # the pack an index row advertising nothing worth reading.
        if any(part.startswith(".") for part in path.parts):
            return False
        resolved = path.resolve()
        return not any(
            resolved == d or d in resolved.parents for d in self._skill_dirs
        )

    def files(self, pack: str) -> list[str]:
        """Every pack-level file, as paths relative to the pack directory."""
        if pack not in self._packs:
            return []
        root = self._base / pack
        if not root.is_dir():
            return []
        return sorted(
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file() and self._is_resource(p)
        )

    def read(self, pack: str, rel: str) -> str | None:
        """Read one pack-level file, or None when it is absent or off-limits.

        Resolves before comparing so ``../`` and symlinks cannot walk out of the
        pack, and refuses anything inside a skill directory -- a skill's own
        files are served as part of that skill, which applies its own scoping.
        """
        if pack not in self._packs:
            return None
        root = (self._base / pack).resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        if not self._is_resource(target):
            return None
        return target.read_text(encoding="utf-8", errors="replace")


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
