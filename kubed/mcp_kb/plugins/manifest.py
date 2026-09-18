"""A plugin's own ``plugin.json``: what it says it ships.

Claude's plugin manifest is the publisher's description of the plugin, so it
is read before the conventions are: a plugin that lists its ``skills`` and
``commands`` is telling us exactly which directories are which, and guessing
from the tree instead is how a repository's ``template/`` skill ends up served.

What it says is *not* trusted as a path. A published manifest is fetched
content, and a component path that escapes the plugin root is dropped rather
than raised: one bad entry must not cost the whole plugin, and nothing outside
the root may be served whatever a manifest asks for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..catalogue.harvest import readable

MANIFEST_FILES = (".claude-plugin/plugin.json", "plugin.json")


@dataclass(frozen=True)
class Manifest:
    name: str | None
    description: str | None
    version: str | None
    keywords: tuple[str, ...]
    skills: tuple[str, ...] | None
    commands: tuple[str, ...] | None


def read_manifest(root: Path) -> Manifest | None:
    """The plugin's manifest, or None when it ships none.

    Found through ``readable``, the guard every read shares: a ``plugin.json``
    that is a symlink out of the root is not this plugin's manifest, and
    ``is_file()`` alone would follow it and let a foreign file's description
    and keywords into the catalogue. A link inside the root still resolves,
    which is the ConfigMap case.
    """
    for name in MANIFEST_FILES:
        path = readable(root, name)
        if path is not None:
            break
    else:
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a JSON object")

    return Manifest(
        name=_text(data.get("name")),
        description=_text(data.get("description")),
        version=_text(data.get("version")),
        keywords=_strings(data.get("keywords"), "keywords") or (),
        skills=paths(data.get("skills"), "skills"),
        commands=command_patterns(data.get("commands")),
    )


def paths(value: object, field: str) -> tuple[str, ...] | None:
    """A manifest's component paths as harvest patterns, escapes dropped.

    Shared with ``marketplace.py``, whose entries carry the same two fields in
    the same spelling -- ``["./skills/loki"]`` -- and must read identically.
    """
    found = _strings(value, field)
    if found is None:
        return None
    return tuple(cleaned for item in found if (cleaned := _inside(item)))


def command_patterns(value: object) -> tuple[str, ...] | None:
    """``paths`` for commands: a file is itself, a directory is every markdown
    file in it -- which is what Claude reads a directory of commands as."""
    found = paths(value, "commands")
    if found is None:
        return None
    return tuple(p if p.endswith(".md") else f"{p}/*.md" for p in found)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _strings(value: object, field: str) -> tuple[str, ...] | None:
    """A field that may be written as one string or as a list of them."""
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError(f"{field} must be a string or a list of strings")


def _inside(text: str) -> str:
    """``text`` as a root-relative path, or ``""`` when it leaves the root."""
    path = text.strip().removeprefix("./")
    if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
        return ""
    return path
