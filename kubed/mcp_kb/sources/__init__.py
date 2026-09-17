"""Sources: turning a fetch into a local directory.

Every filesystem-shaped fetch is *materialised* -- made into a directory on
this machine -- before anything reads it. That is what keeps the catalogue,
the URI grammar, the scoping and the traversal guard unchanged whatever the
backend: they only ever see a ``Path``. A ``file://`` fetch is served in
place; a ``git+…`` one is cloned bare and exported under the cache directory;
a ``webdav+…`` folder is copied into it. The two that copy share one export
mechanism (``export.py``): a tree is written to a new path per version and
never over the one a snapshot is serving.

A ``Fetch`` is one URL at one ref, however many plugins read it: seven
marketplace entries in one repository are one clone. Its ``key`` is what every
error names and its ``slug`` is where the cache puts it -- never a plugin, a
library or a credential.

A fetch that cannot be materialised is an error *value*, not an exception
that escapes: one bad fetch must not take the others down, and ``/health``
reports it instead.
"""

from __future__ import annotations

from pathlib import Path

from ..plugins import Fetch
from .errors import AccessRefused, SourceError
from .file import fingerprint_file, materialise_file
from .git import fingerprint_git, materialise_git
from .webdav import fingerprint_webdav, materialise_webdav

__all__ = [
    "AccessRefused",
    "SourceError",
    "fingerprint",
    "materialise",
]


def materialise(fetch: Fetch, cache: Path) -> Path:
    if fetch.backend == "file":
        return materialise_file(fetch, cache)
    if fetch.backend == "git":
        return materialise_git(fetch, cache)
    if fetch.backend == "webdav":
        return materialise_webdav(fetch, cache)
    raise SourceError(f"{fetch.key}: no backend {fetch.backend!r}")  # pragma: no cover


def fingerprint(fetch: Fetch, cache: Path, root: Path) -> dict:
    if fetch.backend == "file":
        return fingerprint_file(fetch, cache, root)
    if fetch.backend == "git":
        return fingerprint_git(fetch, cache, root)
    if fetch.backend == "webdav":
        return fingerprint_webdav(fetch, cache, root)
    raise SourceError(f"{fetch.key}: no backend {fetch.backend!r}")  # pragma: no cover
