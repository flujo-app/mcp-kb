"""``git+https://``, ``git+http://`` and ``git+file://``.

A git fetch is a repository read at a ref. It is cloned once -- bare, and
shallow wherever the transport allows one -- into ``<cache>/git/<slug>``, the
ref is resolved to a commit, and that commit's tree is exported into
``<cache>/src/<slug>/<commit>``. Everything downstream sees a plain directory,
exactly as a ``file://`` fetch hands it one; nothing about the clone, the
cache or the word "git" reaches a URI, a listing row or a resource body. A
plugin's own subdirectory inside the tree is the catalogue's business, not
this module's: the export is the whole repository, and one export serves
every plugin that reads it.

Three things this module is careful about, each for a reason:

- **A pinned commit never touches the network to fingerprint.** A 40-hex ref
  cannot move, so there is nothing to ask. A branch, a tag or a default HEAD
  costs exactly one ``list_heads`` against the remote per check -- the cheapest
  question git has, and far cheaper than a fetch.
- **An export is never written where one is being served.** Each commit gets
  its own directory, built under a dot-prefixed sibling and renamed on in one
  step. A refresh therefore adds a tree rather than replacing one, so a
  snapshot built against the old export goes on reading it for as long as a
  request is in flight, and a crash can only leave an unreferenced work
  directory. That mechanism is ``export.py``'s and is shared with every other
  backend that copies a tree into the cache; here a version is a commit.
- **A token is a reference, never a value on disk.** Credentials are resolved
  from the environment into ``RemoteCallbacks`` at the moment of the call. The
  URL saved as the clone's remote is the one built from the config -- which
  ``SourceConfig`` refuses to accept with a credential embedded in it -- so
  nothing that lands under the cache, in a log line or in an error carries the
  secret.

One backend-visible difference worth knowing: a symlink in a repository is
exported as a regular file holding the link's target as its text, because
fsspec's ``GitFileSystem`` does not recreate links. It escapes nothing, but the
same tree behind ``file://`` is harvested with the link resolved and then
excluded by the containment guard, so the two backends serve it differently.

Read-only throughout: a fetch is fetched from and never pushed to, and the
working copy that would let anything write back is never created.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pygit2
from fsspec.implementations.git import GitFileSystem

from ..config import ConfigError
from ..plugins import Fetch
from .errors import AccessRefused, SourceError
from .export import Exports

# Written at the root of an export: the commit it holds, then how many files it
# took. Hidden, so harvest.py's dot-file rule keeps it out of every listing by
# itself.
COMMIT_FILE = ".mcp-kb-commit"

SHA = re.compile(r"^[0-9a-f]{40}$")


def materialise_git(fetch: Fetch, cache: Path) -> Path:
    """Clone or reuse, resolve the ref, export that tree, return the export root."""
    repo, fresh = _repository(fetch, cache)
    commit = _resolve(fetch, repo, fresh=fresh)
    return _export(fetch, cache, repo.path, commit)


def fingerprint_git(fetch: Fetch, cache: Path, root: Path) -> dict:
    """The exported commit and its extent, plus what ``ref`` points at right now.

    Two different commits mean the export is behind the remote, which is
    exactly when ``KnowledgeBase.refresh`` should rebuild -- and the same two
    mean it is not, however long ago the clone happened. ``files`` is what
    makes the export's own integrity part of the answer: a tree that lost
    files since it was written has moved as surely as the remote has, and a
    rebuild is what puts it back.

    Read from ``root`` rather than from the cache layout, because an export is
    keyed by its commit: the fingerprint has to describe the tree *this record*
    is served from, not whichever one happens to be newest.
    """
    exports = _exports(fetch, cache)
    exported = exports.exported(root)
    if exported is None:
        raise SourceError(f"{fetch.key}: nothing exported under the cache")
    return {
        "commit": exported[0],
        "files": exports.count(root),
        "ref": fetch.ref,
        "remote": _tip(fetch, cache),
    }


def resolve(fetch: Fetch, cache: Path) -> str:
    """The commit sha ``fetch.ref`` means right now, fetching it if need be."""
    repo, fresh = _repository(fetch, cache)
    return _resolve(fetch, repo, fresh=fresh)


# -- the clone -----------------------------------------------------------------


def _repository(fetch: Fetch, cache: Path) -> tuple[pygit2.Repository, bool]:
    """The bare clone under the cache, and whether this call is what made it.

    A clone that cannot be opened is thrown away and made again rather than
    failing forever: the only way to get one is an interrupted clone, and the
    cache is derived data that is always safe to rebuild.
    """
    bare = cache / "git" / fetch.slug
    if bare.exists():
        try:
            return pygit2.Repository(str(bare)), False
        except pygit2.GitError:
            shutil.rmtree(bare, ignore_errors=True)

    tmp = bare.with_name(bare.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    bare.parent.mkdir(parents=True, exist_ok=True)
    url = fetch.url
    try:
        pygit2.clone_repository(
            url,
            str(tmp),
            bare=True,
            depth=_depth(url),
            callbacks=_callbacks(fetch),
        ).free()
    except pygit2.GitError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise _failed(fetch, "clone failed", exc) from exc
    tmp.rename(bare)
    return pygit2.Repository(str(bare)), True


# How libgit2 words a remote refusing the credentials it was given ("too many
# redirects or authentication replays": it offered them and was asked again)
# or asking for some it was not ("authentication required"). libgit2 raises one
# GitError class for every failure, so its message is all there is to go on.
REFUSED_BY_LIBGIT2 = ("authentication replays", "authentication required")


def _failed(fetch: Fetch, doing: str, exc: pygit2.GitError) -> SourceError:
    """The error for a failed git operation, ``AccessRefused`` when it was one."""
    if any(phrase in str(exc) for phrase in REFUSED_BY_LIBGIT2):
        return AccessRefused(
            f"{fetch.key}: {doing}: the credentials were refused ({exc})"
        )
    return SourceError(f"{fetch.key}: {doing}: {exc}")


def _depth(url: str) -> int:
    """1 -- a shallow clone is all a fetch needs -- except where it is refused.

    libgit2's local transport rejects a shallow fetch outright ("shallow fetch
    is not supported by the local transport"), and a local clone has no
    bandwidth to save anyway, so ``git+file://`` is cloned whole. Every remote
    transport gets the single commit it was asked for.
    """
    return 0 if url.startswith("file://") else 1


def _callbacks(fetch: Fetch) -> pygit2.RemoteCallbacks | None:
    """Credentials for this call only, resolved where they are declared."""
    if fetch.auth is None:
        return None
    try:
        user, password = fetch.auth.pair()
    except ConfigError as exc:
        # Outside this guard a ConfigError is not a SourceError, and one unset
        # variable would abort the whole pass instead of failing its own fetch.
        raise SourceError(f"{fetch.key}: {exc}") from exc
    return pygit2.RemoteCallbacks(credentials=pygit2.UserPass(user, password))


# -- the ref -------------------------------------------------------------------


def _resolve(fetch: Fetch, repo: pygit2.Repository, *, fresh: bool) -> str:
    """The commit ``fetch.ref`` names, fetched into ``repo`` if it is not there.

    ``fresh`` says the clone was just made, in which case its HEAD is by
    definition the remote's and asking again would be a second round trip for
    an answer already in hand.
    """
    ref = fetch.ref
    remote = _remote(fetch, repo)
    if ref is None:
        if fresh:
            return str(_head(fetch, repo))
        _fetch(fetch, remote, ["HEAD"])
        return str(repo.revparse_single("FETCH_HEAD").id)

    if SHA.match(ref):
        if ref not in repo:
            # A shallow clone holds one commit; an older pin is fetched by sha,
            # which is the whole reason the pin is a field and not a URL.
            _fetch(fetch, remote, [ref])
        if ref not in repo:
            raise SourceError(f"{fetch.key}: commit {ref} is not in {fetch.url}")
        return ref

    _fetch(
        fetch,
        remote,
        [
            f"+refs/heads/{ref}:refs/remotes/origin/{ref}",
            f"+refs/tags/{ref}:refs/tags/{ref}",
        ],
    )
    # A branch first, then a tag: a fetched branch lands under origin/, while a
    # tag keeps its own name. Neither resolving is what "not found" means here,
    # because a refspec for a ref the remote does not have is not an error.
    for refish in (f"origin/{ref}", ref):
        try:
            return str(repo.resolve_refish(refish)[0].id)
        except (KeyError, pygit2.GitError):
            continue
    raise SourceError(f"{fetch.key}: ref {ref!r} not found")


def _head(fetch: Fetch, repo: pygit2.Repository) -> pygit2.Oid:
    try:
        return repo.head.target
    except pygit2.GitError as exc:
        raise SourceError(f"{fetch.key}: {fetch.url} has no commits: {exc}") from exc


def _remote(fetch: Fetch, repo: pygit2.Repository) -> pygit2.Remote:
    try:
        return repo.remotes["origin"]
    except KeyError as exc:
        raise SourceError(f"{fetch.key}: the clone has no origin remote") from exc


def _fetch(fetch: Fetch, remote: pygit2.Remote, refspecs: list[str]) -> None:
    depth = _depth(fetch.url)
    try:
        remote.fetch(refspecs, depth=depth, callbacks=_callbacks(fetch))
    except pygit2.GitError as exc:
        raise _failed(fetch, "fetch failed", exc) from exc


def _tip(fetch: Fetch, cache: Path) -> str:
    """What the remote says ``ref`` points at -- one call, and none when pinned."""
    ref = fetch.ref
    if ref is not None and SHA.match(ref):
        return ref

    wanted = ("HEAD",) if ref is None else (f"refs/heads/{ref}", f"refs/tags/{ref}")
    repo, _ = _repository(fetch, cache)
    remote = _remote(fetch, repo)
    try:
        # The callbacks go to list_heads, not to a connect() in front of it:
        # list_heads re-connects unconditionally and with callbacks=None unless
        # it is given some, which silently discards a credentialed handshake
        # made on the line above -- and costs a second round trip to do it.
        heads = {
            head.name: str(head.oid)
            for head in remote.list_heads(callbacks=_callbacks(fetch))
        }
    except pygit2.GitError as exc:
        raise _failed(fetch, f"cannot reach {fetch.url}", exc) from exc
    for name in wanted:
        if name in heads:
            return heads[name]
    raise SourceError(f"{fetch.key}: ref {ref!r} not found")


# -- the export ----------------------------------------------------------------


def _exports(fetch: Fetch, cache: Path) -> Exports:
    """This fetch's export space: one directory per commit, stamped with it."""
    return Exports.under(cache, fetch.slug, stamp=COMMIT_FILE, version=SHA)


def _export(fetch: Fetch, cache: Path, bare: str, commit: str) -> Path:
    """``commit``'s tree at ``<cache>/src/<slug>/<commit>``, renamed in whole."""

    def build(tmp: Path) -> None:
        fs = GitFileSystem(path=bare, ref=commit, skip_instance_cache=True)
        fs.get("", str(tmp), recursive=True)

    return _exports(fetch, cache).ensure(commit, build)
