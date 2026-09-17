"""The on-disk index: what each fetch and each plugin yielded, without re-harvesting.

Cold start reads this file and reconstructs every ``Skill`` and ``FilePrompt``
straight from it, with no filesystem walk and no frontmatter parse -- that is
the whole point of writing one. A row is therefore self-contained: absolute
paths as strings, and no reference back to a live object.

Two kinds of record, because two things are expensive: a ``FetchRecord`` is
one materialised tree (a clone, a copy, a directory) and its fingerprint, and
a ``PluginRecord`` is one plugin's harvest out of such a tree. A skill row
carries no library: which libraries a plugin serves in is assembled from the
config at snapshot time, and a plugin in two libraries is one harvest served
twice.

``Index`` is a frozen dataclass whose two mapping fields are plain ``dict``s.
Python does not stop a caller from mutating a dict reached through a frozen
instance; nothing here needs it to. The type is immutable by convention,
matching every other snapshot in this codebase, and callers that build a new
``Index`` build new mappings rather than mutating one in place.

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
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..config import Config
from .skills import Skill

INDEX_VERSION = 5


@dataclass(frozen=True)
class SkillRow:
    """A harvested skill, flattened to JSON-safe fields.

    No library, plugin, category or tags: those are the library's and the
    plugin's, put back on by ``to_skill`` when a snapshot assigns the plugin
    to a library.
    """

    name: str
    folder: str
    description: str
    path: str

    @classmethod
    def from_skill(cls, skill: Skill) -> SkillRow:
        return cls(
            name=skill.name,
            folder=skill.folder,
            description=skill.description,
            path=str(skill.path),
        )

    def to_skill(
        self,
        *,
        library: str,
        plugin: str,
        root: Path,
        category: str | None,
        tags: frozenset[str],
    ) -> Skill:
        return Skill(
            name=self.name,
            library=library,
            folder=self.folder,
            description=self.description,
            path=Path(self.path),
            plugin=plugin,
            root=root,
            category=category,
            tags=tags,
        )


@dataclass(frozen=True)
class PromptRow:
    """A harvested prompt file: where it is and which dialect it is written in.

    The dialect is recorded because detecting it may have been the config's
    decision (``dialect:`` on the plugin) or the harvest's (a ``commands/``
    tree), and a re-parse at snapshot time has to reach the same answer.
    """

    path: str
    dialect: str


@dataclass(frozen=True)
class FetchRecord:
    """One materialised tree: where it is on disk, and the fingerprint it had.

    ``status``/``error`` carry a failed fetch the way ``sources.materialise``
    already does elsewhere -- a bad fetch is a value on the record, not an
    exception that would keep the other fetches out of the index.

    ``"stale"`` is the third state and the useful one: everything an ``"ok"``
    record has -- a root, a fingerprint -- plus the error from the refresh that
    failed. It is served exactly like ``"ok"``, because the last good tree is
    still on disk and still the best answer available.
    """

    key: str
    status: Literal["ok", "failed", "stale"]
    root: str | None
    fingerprint: dict
    built: str
    error: str | None


@dataclass(frozen=True)
class PluginRecord:
    """One plugin's harvest out of its fetch's tree: every row built from it.

    ``root`` is the plugin root -- the fetch root joined with the plugin's
    subdirectory -- and ``fetch`` the key of the record it was harvested from,
    which is what decides whether this record can be reused when that one is.
    A plugin fails on its own when its root is not a directory, its manifest
    does not parse, or its fetch failed; the error says which.
    """

    id: str
    fetch: str
    root: str | None
    status: Literal["ok", "failed"]
    error: str | None
    built: str
    skills: tuple[SkillRow, ...]
    prompts: tuple[PromptRow, ...]
    files: tuple[str, ...]
    skill_dirs: tuple[str, ...]
    # What the harvest found and could not load -- a prompt file that does not
    # parse -- as ``{"path", "reason"}`` rows. Kept on the record because the
    # harvest is the only pass that sees such a file: its rows never name it.
    skipped: tuple[dict[str, str], ...] = ()


def _fetch_record_from_dict(raw: dict) -> FetchRecord:
    return FetchRecord(
        key=raw["key"],
        status=raw["status"],
        root=raw["root"],
        fingerprint=raw["fingerprint"],
        built=raw["built"],
        error=raw["error"],
    )


def _plugin_record_from_dict(raw: dict) -> PluginRecord:
    return PluginRecord(
        id=raw["id"],
        fetch=raw["fetch"],
        root=raw["root"],
        status=raw["status"],
        error=raw["error"],
        built=raw["built"],
        skills=tuple(SkillRow(**s) for s in raw["skills"]),
        prompts=tuple(PromptRow(**p) for p in raw["prompts"]),
        files=tuple(raw["files"]),
        skill_dirs=tuple(raw["skill_dirs"]),
        skipped=tuple(_skipped_row(row) for row in raw["skipped"]),
    )


def _skipped_row(row: object) -> dict[str, str]:
    """One ``{"path", "reason"}`` row, exactly -- anything else is a bad index.

    Every other field is shaped by constructing a dataclass, which refuses a
    wrong one; this one is a plain dict, so it is checked by hand, and a row
    the log and ``/health`` would index into must not reach them half-formed.
    """
    if (
        not isinstance(row, dict)
        or set(row) != {"path", "reason"}
        or not all(isinstance(v, str) for v in row.values())
    ):
        raise ValueError(f"not a skipped row: {row!r}")
    return {"path": row["path"], "reason": row["reason"]}


@dataclass(frozen=True)
class Index:
    """The whole on-disk record: one JSON file, one atomic write."""

    version: int
    built: str
    config_hash: str
    fetches: dict[str, FetchRecord]
    plugins: dict[str, PluginRecord]

    def write(self, path: Path) -> None:
        """Write to a unique temp file beside ``path``, then ``os.replace`` it.

        A reader never sees a half-written file: ``os.replace`` is a single
        filesystem rename, so the index at ``path`` is either the previous
        complete one or this complete one, never a partial write. The temp
        file comes from ``tempfile.mkstemp`` rather than a fixed sibling name,
        so two writers in the same directory can never race for it.
        """
        fd, name = tempfile.mkstemp(
            dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
        )
        tmp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(self), sort_keys=True))
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
        if not isinstance(raw.get("fetches"), dict) or not isinstance(
            raw.get("plugins"), dict
        ):
            return None
        try:
            return cls(
                version=raw["version"],
                built=raw["built"],
                config_hash=raw["config_hash"],
                fetches={
                    key: _fetch_record_from_dict(rec)
                    for key, rec in raw["fetches"].items()
                },
                plugins={
                    pid: _plugin_record_from_dict(rec)
                    for pid, rec in raw["plugins"].items()
                },
            )
        except (KeyError, TypeError, AttributeError, ValueError):
            return None


def config_hash(config: Config) -> str:
    """A stable fingerprint of the config, independent of key order."""
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def now() -> str:
    """The current time, ISO-8601 UTC at seconds precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
