"""The on-disk index: what each source yielded, without re-harvesting it.

Cold start reads this file and reconstructs every ``Skill`` and ``FilePrompt``
straight from it, with no filesystem walk and no frontmatter parse -- that is
the whole point of writing one. A row is therefore self-contained: absolute
paths as strings, tags sorted so two harvests of the same tree serialise
identically, and no reference back to a live object.

``Index`` is a frozen dataclass whose one field, ``sources``, is a plain
``dict``. Python does not stop a caller from mutating a dict reached through a
frozen instance; nothing here needs it to. The type is immutable by
convention, matching every other snapshot in this codebase, and callers that
build a new ``Index`` build a new ``sources`` mapping rather than mutating one
in place.

``config_hash`` and the ``version`` field are cold start's two questions before
it trusts anything in the file: "was this built from the config I have now",
and "do I still know how to read this shape". Either mismatch means rebuild
from scratch, so ``Index.read`` folds every way of failing those questions --
missing file, unparsable JSON, a wrong or absent version, a shape that does
not match the dataclasses below -- into a single ``None``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .config import Config
from .prompts import FilePrompt
from .skills import Skill

INDEX_VERSION = 1


@dataclass(frozen=True)
class SkillRow:
    """A ``Skill``, flattened to JSON-safe fields."""

    name: str
    pack: str
    group: str
    description: str
    path: str
    source: str
    tags: tuple[str, ...]

    @classmethod
    def from_skill(cls, skill: Skill) -> SkillRow:
        return cls(
            name=skill.name,
            pack=skill.pack,
            group=skill.group,
            description=skill.description,
            path=str(skill.path),
            source=skill.source,
            tags=tuple(sorted(skill.tags)),
        )

    def to_skill(self) -> Skill:
        return Skill(
            name=self.name,
            pack=self.pack,
            group=self.group,
            description=self.description,
            path=Path(self.path),
            source=self.source,
            tags=frozenset(self.tags),
        )


@dataclass(frozen=True)
class PromptRow:
    """A ``FilePrompt``, flattened to JSON-safe fields.

    ``FilePrompt`` gains its own ``path`` attribute in a later task; until then
    the harvester knows the path a prompt came from and this takes it as an
    argument rather than reading it off the prompt.
    """

    path: str
    pack: str
    source: str
    tags: tuple[str, ...]

    @classmethod
    def of(cls, path: Path, prompt: FilePrompt) -> PromptRow:
        return cls(
            path=str(path),
            pack=prompt.pack,
            source=prompt.source,
            tags=tuple(sorted(prompt.tags)),
        )


@dataclass(frozen=True)
class SourceRecord:
    """One source's harvest: its fingerprint and every row built from it.

    ``status``/``error`` carry a failed source the way ``sources.materialise``
    already does elsewhere -- a bad source is a value on the record, not an
    exception that would keep the other sources out of the index.

    ``"stale"`` is the third state and the useful one: everything a ``"ok"``
    record has -- a root, rows, a fingerprint -- plus the error from the
    refresh that failed. It is served exactly like ``"ok"``, because the last
    good harvest is still on disk and still the best answer available.
    """

    name: str
    status: Literal["ok", "failed", "stale"]
    library: str
    root: str | None
    fingerprint: dict
    built: str
    error: str | None
    skills: tuple[SkillRow, ...]
    prompts: tuple[PromptRow, ...]
    files: tuple[str, ...]
    skill_dirs: tuple[str, ...]


def _source_record_from_dict(raw: dict) -> SourceRecord:
    return SourceRecord(
        name=raw["name"],
        status=raw["status"],
        library=raw["library"],
        root=raw["root"],
        fingerprint=raw["fingerprint"],
        built=raw["built"],
        error=raw["error"],
        skills=tuple(
            SkillRow(**{**s, "tags": tuple(s["tags"])}) for s in raw["skills"]
        ),
        prompts=tuple(
            PromptRow(**{**p, "tags": tuple(p["tags"])}) for p in raw["prompts"]
        ),
        files=tuple(raw["files"]),
        skill_dirs=tuple(raw["skill_dirs"]),
    )


@dataclass(frozen=True)
class Index:
    """The whole on-disk record: one JSON file, one atomic write."""

    version: int
    built: str
    config_hash: str
    sources: dict[str, SourceRecord]

    def write(self, path: Path) -> None:
        """Write as ``<path>.tmp`` then ``os.replace`` it onto ``path``.

        A reader never sees a half-written file: ``os.replace`` is a single
        filesystem rename, so the index at ``path`` is either the previous
        complete one or this complete one, never a partial write.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)  # noqa: PTH105 - the atomic rename tests monkeypatch

    @classmethod
    def read(cls, path: Path) -> Index | None:
        """The index at ``path``, or ``None`` when it cannot be trusted.

        Missing, unparsable, the wrong version, or a shape that does not match
        the dataclasses above all collapse to the same ``None`` -- every case
        means "rebuild from scratch", so the cause does not need to travel any
        further than a log line at the call site.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
            return None
        if not isinstance(raw.get("sources"), dict):
            return None
        try:
            sources = {
                name: _source_record_from_dict(rec)
                for name, rec in raw["sources"].items()
            }
            return cls(
                version=raw["version"],
                built=raw["built"],
                config_hash=raw["config_hash"],
                sources=sources,
            )
        except (KeyError, TypeError, AttributeError, ValueError):
            return None


def config_hash(config: Config) -> str:
    """A stable fingerprint of the config, independent of key order."""
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def now() -> str:
    """The current time, ISO-8601 UTC at seconds precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
