"""``webdav+https://`` and ``webdav+http://`` -- a WebDAV folder, Nextcloud above all.

The configured URL *is* the folder, so it is the client's base and every path
under it is relative. The folder is copied into ``<cache>/src/<slug>/<version>``
and everything downstream sees a plain directory, exactly as a ``file://``
fetch hands it one: nothing about WebDAV, the cache or the server's address
reaches a URI, a listing row or a resource body.

Three things this module is careful about:

- **The version is the ETag set.** One recursive PROPFIND prices the whole
  folder, and the digest of ``{path: etag}`` is what names the export. That
  makes "has it moved" and "what do I call the copy" the same question asked
  once: a folder that has not changed resolves to the export already on disk
  and is not downloaded again, and one that has changed is copied *beside* the
  tree being served rather than over it (``export.py``, shared with git).
- **A credential is a reference until the client exists.** The password is
  resolved from the environment into the httpx auth pair inside ``client`` and
  nowhere else. It is never part of the URL -- ``SourceConfig`` refuses one that
  is -- so nothing that lands under the cache, in a log line or in the errors
  ``/health`` publishes can carry it. An HTTP failure is reported as its status
  and nothing more.
- **A live read replaces one file, atomically.** ``fetch_file`` is the other
  half of ``cache: live``: one PROPFIND, and only if the ETag moved, one GET
  into a temporary file in ``.work/`` beside the export that is then renamed
  over the local copy. A reader holding the old file reads the old file to its
  end, and a download that is killed half way through leaves nothing inside
  the export for the next pass to read as a missing file.

Read-only throughout: a fetch is copied from and never written to.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from webdav4.client import Client, ClientError, HTTPError
from webdav4.fsspec import WebdavFileSystem

from ..config import ConfigError
from ..plugins import Fetch
from .errors import AccessRefused, SourceError
from .export import WORK_PREFIX, Exports, workspace

if TYPE_CHECKING:
    from httpx import URL

# Written at the root of an export: what every file's ETag was when it was
# copied, which is what a live read revalidates against. Hidden, so harvest.py's
# dot-file rule keeps it out of every listing by itself.
ETAGS_FILE = ".mcp-kb-etags.json"

# The export's completion stamp: the version it holds, then how many files it
# took. export.py writes it, last of all.
VERSION_FILE = ".mcp-kb-version"

VERSION = re.compile(r"^[0-9a-f]{64}$")


class _Client(Client):
    """webdav4's client, with the path percent-encoded on its way into a URL.

    webdav4 reads a listing's ``href`` through httpx, which decodes it, and
    then re-addresses that decoded path with ``URL.copy_with(path=...)``, which
    refuses a ``#`` or a ``?``. A hash in a note's filename is ordinary in
    Nextcloud and took the whole folder down with it. Encoding here is the one
    place both halves meet: every path webdav4 hands back is still the decoded
    one, and an already-encoded escape is left alone rather than doubled.
    """

    def join_url(self, path: str, add_trailing_slash: bool = False) -> URL:
        return super().join_url(
            quote(path, safe="/"), add_trailing_slash=add_trailing_slash
        )


def client(fetch: Fetch, *, timeout: float | None = None) -> WebdavFileSystem:
    """A client for this fetch, credentials resolved at the moment of building it.

    ``skip_instance_cache`` keeps it out of fsspec's global instance cache:
    nothing carrying a password belongs in a process-wide dictionary, and a
    fresh instance is also a fresh view of a server whose whole point is that
    it changes underneath us.

    ``timeout`` is per HTTP operation and left to httpx's own default here --
    an index-time copy of a large folder is allowed to take its time. A read
    is not, and ``live.py`` passes one; see ``REVALIDATE_TIMEOUT``.
    """
    if fetch.auth is None:
        # SourceConfig refuses an anonymous webdav source; a Fetch built by
        # hand could still arrive without one, and that is its own failure.
        raise SourceError(f"{fetch.key}: a webdav fetch needs auth")
    try:
        user, password = fetch.auth.pair()
    except ConfigError as exc:
        # Config, not authentication: the variable was never set, so there is
        # nothing to ask the server and no 401 to report. Outside this guard one
        # unset variable aborts the pass instead of failing its own fetch.
        raise SourceError(f"{fetch.key}: {exc}") from exc
    opts = {} if timeout is None else {"timeout": timeout}
    return WebdavFileSystem(
        fetch.url,
        client=_Client(fetch.url, auth=(user, password), **opts),
        skip_instance_cache=True,
    )


def materialise_webdav(fetch: Fetch, cache: Path) -> Path:
    """Copy the folder into the cache under the digest of its ETags, and return it."""
    fs = client(fetch)
    etags = _etags(fetch, fs)

    def build(tmp: Path) -> None:
        with _reporting(fetch, "copy"):
            fs.get("", str(tmp), recursive=True)
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / ETAGS_FILE).write_text(
            json.dumps(etags, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )

    return _exports(fetch, cache).ensure(_version(etags), build)


def fingerprint_webdav(fetch: Fetch, cache: Path, root: Path) -> dict:
    """What the folder is upstream right now, and what the local copy holds.

    ``remote`` is the digest of every file's ETag and ``remote_files`` how many
    there are -- which is the whole of what change detection needs, an edit
    moving the digest and an add or a delete moving both. The map itself is
    *not* here: a fingerprint is published in ``/health`` and written to
    ``index.json``, and a folder's entire ``{path: etag}`` map has no business
    in either. It is on disk at the export's root, which is where a live read
    reads it from.

    ``exported`` and ``files`` make the copy's own integrity part of the
    answer: a tree that lost files since it was written has moved as surely as
    the server has, and a rebuild is what puts it back. The two halves are
    named as git's are -- ``exported``/``files`` for the tree being served,
    ``remote`` for what the server says now -- and they are equal when the copy
    is level with the folder.
    """
    exports = _exports(fetch, cache)
    exported = exports.exported(root)
    if exported is None:
        raise SourceError(f"{fetch.key}: nothing exported under the cache")
    etags = _etags(fetch, client(fetch))
    return {
        "exported": exported[0],
        "files": exports.count(root),
        "remote": _version(etags),
        "remote_files": len(etags),
    }


def fetch_file(
    fetch: Fetch,
    root: Path,
    rel: str,
    etag: str | None = None,
    *,
    fs: WebdavFileSystem | None = None,
) -> str | None:
    """Revalidate one file against the server; re-download it only if it moved.

    ``etag`` is what the file was when it was copied -- the caller's record of
    it, or the export's own if the caller has none. Returns the ETag the file
    has now, which is the same object when nothing changed, or None when the
    file is gone upstream: a deleted file is not a deleted skill, and the local
    copy goes on being served until a refresh rebuilds the fetch without it.

    ``fs`` lets a caller reuse one client across many reads, which is the whole
    difference between a live fetch and a slow one.
    """
    target = _inside(fetch, root, rel)
    fs = fs or client(fetch)
    recorded = etag if etag is not None else recorded_etags(root).get(rel)

    try:
        info = fs.info(rel)
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise SourceError(f"{fetch.key}: {_failure(exc)} for {rel}") from exc

    current = _etag(info)
    if current == recorded and target.is_file():
        return current

    target.parent.mkdir(parents=True, exist_ok=True)
    # Staged beside the export, never inside it: a download killed part way
    # through would otherwise leave a file the export's own count includes and
    # the collector cannot reach, which makes a whole tree look truncated for
    # good. Same filesystem, so the replace below is still one atomic step.
    handle, name = tempfile.mkstemp(dir=workspace(root.parent), prefix=WORK_PREFIX)
    os.close(handle)
    tmp = Path(name)
    try:
        with _reporting(fetch, f"fetch of {rel}"):
            fs.get_file(rel, str(tmp))
        # Atomic: a reader that already opened the old file reads the old file
        # to its end, and no reader ever sees a half-written one.
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return current


# -- the folder ----------------------------------------------------------------


def _exports(fetch: Fetch, cache: Path) -> Exports:
    """This fetch's export space: one directory per ETag set, stamped with it."""
    return Exports.under(cache, fetch.slug, stamp=VERSION_FILE, version=VERSION)


def _etags(fetch: Fetch, fs: WebdavFileSystem) -> dict[str, str]:
    """``{path: etag}`` for every file in the folder, in path order.

    One recursive listing, which is one PROPFIND per directory -- webdav4
    passes the ETag through from the same response that enumerates the
    children, so a listing prices every file's freshness for free. Sorted, so
    two equal folders produce two equal dicts and one digest.
    """
    with _reporting(fetch, "listing"):
        try:
            fs.info("")
        except FileNotFoundError:
            # Asked first because a listing of a folder that is not there is
            # not an error to webdav4: it is an empty folder, and the copy
            # that follows fails on it with nothing an operator can act on.
            raise SourceError(
                f"{fetch.key}: the folder {_folder(fetch)} does not exist"
            ) from None
        found = fs.find("", detail=True)
    return {
        path: _etag(info)
        for path, info in sorted(found.items())
        if info.get("type") == "file"
    }


def _etag(info: dict) -> str:
    """The file's ETag, or what stands in for one where a server sends none.

    WebDAV does not require an ETag. Nextcloud always sends one; a server that
    does not still has a modification time and a size, and a version built from
    those changes when the file does -- which is all either caller needs.
    """
    return info.get("etag") or f"{info.get('modified')}:{info.get('size')}"


def _version(etags: dict[str, str]) -> str:
    """A name for the folder in this exact state: the digest of its ETag map.

    Content-addressed the way a commit is, and for the same reason: two exports
    with the same name hold the same bytes, so an unchanged folder resolves to
    the tree already on disk instead of being copied over it. An edit moves the
    file's ETag, and an add or a delete moves the set -- either way the digest
    moves, and the new copy goes to a directory of its own.
    """
    digest = hashlib.sha256()
    for path in sorted(etags):
        digest.update(f"{path}\0{etags[path]}\n".encode())
    return digest.hexdigest()


def recorded_etags(root: Path) -> dict[str, str]:
    """The ETags the export was copied with, as written at its root.

    The one place the whole map lives, and where ``live.py`` picks it up: the
    fingerprint carries only its digest.
    """
    try:
        return json.loads((root / ETAGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _inside(fetch: Fetch, root: Path, rel: str) -> Path:
    """``root/rel``, refused if it leaves the export.

    ``rel`` comes from a listing the server controls, and a live read is the
    one path that writes into a materialised tree.
    """
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise SourceError(f"{fetch.key}: {rel!r} is outside the export")
    return target


# -- failures ------------------------------------------------------------------


@contextlib.contextmanager
def _reporting(fetch: Fetch, doing: str) -> Iterator[None]:
    """Turn whatever the transport raises into a SourceError naming this fetch.

    A 401 or a 403 is ``AccessRefused``: the server is there and said no to
    this account, which no amount of retrying changes.
    """
    try:
        yield
    except SourceError:
        raise
    except Exception as exc:
        message = f"{fetch.key}: {doing} failed: {_failure(exc)}"
        if isinstance(exc, HTTPError) and exc.status_code in REFUSED:
            raise AccessRefused(message) from exc
        raise SourceError(message) from exc


# What each refusal means for the account, which is what an operator acts on.
REFUSED = {
    401: "the credentials were refused",
    403: "the account may not read this folder",
}


def _failure(exc: Exception) -> str:
    """What to say about a failed request: its status, or the transport's word.

    Never the URL and never the credentials -- the status is what an operator
    acts on, and a 401 in /health says "the app password is wrong" without
    publishing what was tried.
    """
    if isinstance(exc, HTTPError):
        meaning = REFUSED.get(exc.status_code)
        return f"HTTP {exc.status_code}" + (f": {meaning}" if meaning else "")
    if isinstance(exc, ClientError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _folder(fetch: Fetch) -> str:
    """The folder's path on its server: the URL without its scheme and host.

    The host is left out with the rest of the URL for the same reason the URL
    always is, and the path is what names the folder to create.
    """
    return urlsplit(fetch.url).path or "/"
