"""``cache: live``: bringing one file up to date with its server as it is read.

A mirrored source is copied into the cache and read from disk, which is why a
request touches no network at all. That is the right trade for a git repository
and the wrong one for a folder somebody is editing: a note changed in Nextcloud
should be readable now, not after the next refresh.

So a source may say ``cache: live``, and then the read path goes through here
first. Given the path it is about to serve, the revalidator finds the live
fetch that owns it, asks the server what that one file's ETag is, and fetches
it only if the ETag moved. One PROPFIND, and on a change one GET -- not the
folder, not the index, one file.

Three properties this has to keep, and they are why it is ~80 lines rather than
a cache library:

- **It degrades to the copy on disk, cheaply.** Every failure is logged and
  swallowed. A server that is down, a password that was rotated, a proxy that
  is confused: none of them may turn a read that a complete local copy could
  have answered into a read that fails. Nor may they make it *slow* -- a read
  holds the event loop, so a Nextcloud that accepts connections and never
  answers would otherwise cost the whole server a timeout per read. Hence the
  explicit client timeout and the cooldown below: the first read that times
  out is the only one that pays for it.
- **It is per-file, so it cannot see a new one.** A file that did not exist when
  the folder was indexed has no URI, and nothing asks to read it. New and
  deleted files are a ``refresh:`` interval's job, or ``POST /reindex``.
- **It belongs to its snapshot.** A refresh exports the folder to a *new*
  directory, so the roots in here are the previous generation's the moment one
  finishes. ``build_snapshot`` builds a revalidator alongside the snapshot that
  uses it, and the old one is dropped with the old snapshot.

fsspec ships ``filecache``, which is the obvious alternative and does not fit:
it stores files under hashed names in a directory of its own, and everything
downstream of a fetch here is a real directory tree of ``Path``s.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..plugins import Fetch
from .webdav import client, fetch_file, recorded_etags

if TYPE_CHECKING:
    from webdav4.fsspec import WebdavFileSystem

log = logging.getLogger(__name__)

# How long a file's ETag is taken on trust once it has been checked. Reading a
# skill is usually reading several of its files, and a client that lists and
# then reads asks for the same bytes twice within the same second; without a
# floor every one of those is a round trip. It is also the whole staleness
# bound of a live fetch: nothing is ever more than this far behind.
TTL_SECONDS = 2.0

# What a revalidation may cost. httpx defaults to 5 seconds, which is more than
# twice the whole staleness bound above and is paid on the request path -- by
# every other fetch's reads too, since a read is synchronous. Per HTTP
# operation, so a large file downloading steadily is unaffected; what this
# bounds is a server that has gone quiet.
REVALIDATE_TIMEOUT = 1.0

# How long a fetch is left unrevalidated after one of its reads failed. The
# timeout above bounds a single read; this is what stops a wedged server
# costing anything at all after the first. Short, because the cost of being
# wrong is one revalidation's delay and the gain is that a folder comes back to
# life by itself.
COOLDOWN_SECONDS = 30.0


def is_live(fetch: Fetch) -> bool:
    """Whether reads of ``fetch`` revalidate. Only a WebDAV source can say so."""
    return fetch.backend == "webdav" and fetch.live


@dataclass
class _Live:
    """One live fetch: what it is, where its copy is, what its files were.

    ``etags`` starts as the map written into the export at index time and is
    updated in place as files move, so it always says what the tree on disk
    actually holds. ``fs`` is built on the first read and kept: a client per
    read is a TCP connection and a TLS handshake per read.
    """

    fetch: Fetch
    root: Path
    etags: dict[str, str]
    fs: WebdavFileSystem | None = None
    revalidated: int = 0
    fetched: int = 0
    # The monotonic clock reading this fetch may be asked again at. Set when a
    # revalidation fails, cleared when one succeeds.
    cooling_until: float = 0.0


class Revalidator:
    """The live fetches of one snapshot, and the last time each file was priced.

    Built from the records a snapshot is assembled from, and thrown away with
    it. Counters are plain ``int`` increments: the request path is the event
    loop and the worst a race could cost is a miscounted number in ``/health``.
    """

    def __init__(
        self,
        fetches: Sequence[tuple[Fetch, Path]],
        *,
        ttl: float | None = None,
        cooldown: float | None = None,
    ):
        self._sources = [
            _Live(fetch=fetch, root=root.resolve(), etags=recorded_etags(root))
            for fetch, root in fetches
        ]
        # Read from the module rather than defaulted in the signature, so the
        # value a test sets is the value a snapshot built afterwards uses.
        self._ttl = TTL_SECONDS if ttl is None else ttl
        self._cooldown = COOLDOWN_SECONDS if cooldown is None else cooldown
        self._checked: dict[tuple[str, str], float] = {}

    def revalidate(self, path: Path) -> None:
        """Bring ``path`` level with its server, or leave it exactly as it is."""
        owned = self._owner(path)
        if owned is None:
            return
        live, rel = owned

        now = time.monotonic()
        if now < live.cooling_until:
            return
        key = (live.fetch.key, rel)
        if now - self._checked.get(key, float("-inf")) < self._ttl:
            return
        # Stamped before the request as well as after it. Before, so a server
        # that has stopped answering is not asked again by every read that
        # follows; after, so the TTL runs from the moment the answer came back
        # -- stamped only before, a request that took longer than the TTL would
        # leave the same file due for another the instant it returned.
        self._checked[key] = now

        try:
            etag = fetch_file(
                live.fetch,
                live.root,
                rel,
                live.etags.get(rel),
                fs=self._client(live),
            )
        except Exception as exc:  # noqa: BLE001 - a flaky server is not a failed read
            self._checked[key] = time.monotonic()
            live.cooling_until = time.monotonic() + self._cooldown
            # fetch_file names the fetch and the file already; saying either
            # of them twice reads as two of them.
            reason = str(exc).removeprefix(f"{live.fetch.key}: ")
            reason = reason.removesuffix(f" for {rel}")
            log.warning("%s: revalidating %s failed: %s", live.fetch.key, rel, reason)
            return

        self._checked[key] = time.monotonic()
        live.cooling_until = 0.0
        live.revalidated += 1
        if etag is not None and etag != live.etags.get(rel):
            live.etags[rel] = etag
            live.fetched += 1

    def stats(self, key: str) -> dict:
        """What reads of the fetch ``key`` have asked of its server, for ``/health``.

        ``revalidated`` counts the files priced against the server, and
        ``fetched`` the ones that had moved and were downloaded again -- which
        together say whether edits are actually flowing. ``cooling`` appears
        while a failure has this fetch's reads suspended, which is the one
        state where those two counters stop moving for a reason that is not
        "nothing changed".
        """
        for live in self._sources:
            if live.fetch.key == key:
                return {
                    "revalidated": live.revalidated,
                    "fetched": live.fetched,
                    **(
                        {"cooling": True}
                        if time.monotonic() < live.cooling_until
                        else {}
                    ),
                }
        return {}

    def _owner(self, path: Path) -> tuple[_Live, str] | None:
        """The live fetch ``path`` sits under, and its path within it."""
        resolved = path.resolve()
        for live in self._sources:
            if resolved.is_relative_to(live.root):
                return live, resolved.relative_to(live.root).as_posix()
        return None

    def _client(self, live: _Live) -> WebdavFileSystem:
        """This fetch's client, built on first use.

        Not at construction: building one resolves a credential, and a snapshot
        must be assemblable without one -- a live fetch whose password is
        missing fails its reads and logs, it does not fail the whole rebuild.
        """
        if live.fs is None:
            live.fs = client(live.fetch, timeout=REVALIDATE_TIMEOUT)
        return live.fs
