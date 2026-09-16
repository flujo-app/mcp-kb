"""Sources: turning a config entry into a local directory.

Every filesystem-shaped source is *materialised* -- made into a directory on
this machine -- before anything reads it. That is what keeps the catalogue,
the URI grammar, the scoping and the traversal guard unchanged whatever the
backend: they only ever see a ``Path``. A ``file://`` source is served in
place; a remote one is copied under the cache directory.

A source that cannot be materialised is an error *value*, not an exception
that escapes: one bad source must not take the others down, and ``/health``
reports it instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config, FileSource, Source
from .errors import SourceError
from .file import materialise_file

log = logging.getLogger(__name__)

__all__ = ["SourceError", "materialise", "materialise_all"]


def materialise(source: Source, cache: Path) -> Path:
    if isinstance(source, FileSource):
        return materialise_file(source, cache)
    raise SourceError(f"{source.name}: no handler for {source.url}")  # pragma: no cover


def materialise_all(config: Config, cache: Path) -> dict[str, Path | SourceError]:
    got: dict[str, Path | SourceError] = {}
    for source in config.sources:
        try:
            got[source.name] = materialise(source, cache)
        except SourceError as exc:
            log.warning("source %s skipped: %s", source.name, exc)
            got[source.name] = exc
    return got
