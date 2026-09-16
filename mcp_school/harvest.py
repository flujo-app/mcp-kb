"""Turn a source directory into the things the catalogue serves.

A source is a directory (``sources/`` produced it); what to serve out of it is
a set of globs per kind, and the conventions below are the defaults. Setting a
kind *replaces* its default rather than appending to it -- an append would
leave no way to stop serving a convention -- and an empty list turns a kind
off.

Everything found is resolved and checked to lie inside the root, the same
guard ``uris.py`` applies on read, so a glob like ``../**`` finds nothing.
Dot directories are skipped, except the three that agent tooling conventionally
lives in.

Write a files glob as ``dir/**/*``, not ``dir/**`` -- a trailing ``**`` matches
directories only on Python versions before 3.13.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .config import Include

MAIN_FILE = "SKILL.md"

DEFAULTS: dict[str, tuple[str, ...]] = {
    "skills": (
        "skills/**/SKILL.md",
        ".github/skills/*/SKILL.md",
        ".claude/skills/*/SKILL.md",
        ".agents/skills/*/SKILL.md",
    ),
    "prompts": ("prompts/**/*.md", ".github/prompts/*.prompt.md", "commands/*.md"),
    "instructions": (
        ".github/copilot-instructions.md",
        ".github/instructions/*.instructions.md",
        "AGENTS.md",
        "CLAUDE.md",
    ),
    "agents": (".github/agents/*.agent.md", "agents/*.md"),
    "files": (),
}

# The directories that hold skills by convention. A skill directly under one of
# these has no group of its own; anything deeper is grouped by its parent.
SKILL_ROOTS: tuple[str, ...] = (
    "",
    "skills",
    ".github/skills",
    ".claude/skills",
    ".agents/skills",
)

CONVENTIONAL_DOTDIRS: frozenset[str] = frozenset({".github", ".claude", ".agents"})


def patterns(kind: str, include: Include) -> tuple[str, ...]:
    explicit = getattr(include, kind)
    return tuple(explicit) if explicit is not None else DEFAULTS[kind]


def _hidden(rel: Path) -> bool:
    return any(
        part.startswith(".") and part not in CONVENTIONAL_DOTDIRS for part in rel.parts
    )


def files(root: Path, kind: str, include: Include) -> list[Path]:
    """Every regular file matching the kind's globs, resolved, inside root, sorted."""
    base = root.resolve()
    found: set[Path] = set()
    for pattern in patterns(kind, include):
        for hit in base.glob(pattern):
            # A pattern like "../**" can walk out of base and straight back in
            # -- resolve() then quietly collapses the ".." and the hit looks
            # like a plain interior file. Reject the escape before resolving.
            # config.py's Include validator refuses such a pattern before it
            # ever reaches here; this is defence in depth, not the first line.
            if ".." in hit.relative_to(base).parts:
                continue
            target = hit.resolve()
            if not target.is_file() or not target.is_relative_to(base):
                continue
            if _hidden(target.relative_to(base)):
                continue
            found.add(target)
    return sorted(found)


def skill_dirs(root: Path, include: Include) -> list[Path]:
    return [f.parent for f in files(root, "skills", include) if f.name == MAIN_FILE]


def prompt_files(root: Path, include: Include) -> list[Path]:
    return files(root, "prompts", include)


def pack_files(root: Path, include: Include, skill_dirs: Sequence[Path]) -> list[str]:
    """Pack-level files as root-relative posix paths, never one inside a skill."""
    base = root.resolve()
    inside = tuple(d.resolve() for d in skill_dirs)
    return [
        f.relative_to(base).as_posix()
        for f in files(root, "files", include)
        if not any(f == d or d in f.parents for d in inside)
    ]


def group_of(skill_dir: Path, root: Path) -> str | None:
    """The directory containing a skill, unless that is one of the skill roots."""
    parent = skill_dir.resolve().parent
    rel = parent.relative_to(root.resolve()).as_posix()
    return None if rel in SKILL_ROOTS or rel == "." else parent.name
