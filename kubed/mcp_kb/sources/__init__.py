"""Sources: turning a config entry into a local directory.

Every filesystem-shaped source is *materialised* -- made into a directory on
this machine -- before anything reads it. That is what keeps the catalogue,
the URI grammar, the scoping and the traversal guard unchanged whatever the
backend: they only ever see a ``Path``. A ``file://`` source is served in
place; a ``git+…`` one is cloned bare and exported under the cache directory;
a ``webdav+…`` folder is copied into it. The two that copy share one export
mechanism (``export.py``): a tree is written to a new path per version and
never over the one a snapshot is serving.

A source that cannot be materialised is an error *value*, not an exception
that escapes: one bad source must not take the others down, and ``/health``
reports it instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config, FileSource, GitSource, Source, WebdavSource
from .errors import SourceError
from .file import fingerprint_file, materialise_file
from .git import fingerprint_git, materialise_git
from .webdav import fingerprint_webdav, materialise_webdav

log = logging.getLogger(__name__)

__all__ = ["SourceError", "fingerprint", "materialise", "materialise_all"]


def materialise(source: Source, cache: Path) -> Path:
    if isinstance(source, FileSource):
        return materialise_file(source, cache)
    if isinstance(source, GitSource):
        return materialise_git(source, cache)
    if isinstance(source, WebdavSource):
        return materialise_webdav(source, cache)
    raise SourceError(f"{source.name}: no handler for {source.url}")  # pragma: no cover


def fingerprint(source: Source, cache: Path, root: Path) -> dict:
    if isinstance(source, FileSource):
        return fingerprint_file(source, cache, root)
    if isinstance(source, GitSource):
        return fingerprint_git(source, cache, root)
    if isinstance(source, WebdavSource):
        return fingerprint_webdav(source, cache, root)
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
