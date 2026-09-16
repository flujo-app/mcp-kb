"""``git+https://``, ``git+http://``, ``git+file://`` and ``github://``.

A git source is a repository read at a ref. It is cloned once -- bare, and
shallow wherever the transport allows one -- into ``<cache>/git/<name>``, the
ref is resolved to a commit, and that commit's tree is exported into
``<cache>/src/<name>/<commit>``. Everything downstream sees a plain directory,
exactly as a ``file://`` source hands it one; nothing about the clone, the
cache or the word "git" reaches a URI, a listing row or a resource body.

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
  directory. ``_collect`` sweeps those, and the exports nothing can still be
  reading, on the next pass.
- **A token is a reference, never a value on disk.** Credentials are resolved
  from the environment into ``RemoteCallbacks`` at the moment of the call. The
  URL saved as the clone's remote is the one from the config -- which
  ``GitSource`` refuses to accept with a credential embedded in it -- so
  nothing that lands under the cache, in a log line or in an error carries the
  secret.

One backend-visible difference worth knowing: a symlink in a repository is
exported as a regular file holding the link's target as its text, because
fsspec's ``GitFileSystem`` does not recreate links. It escapes nothing, but the
same tree behind ``file://`` is harvested with the link resolved and then
excluded by the containment guard, so the two backends serve it differently.

Read-only throughout: a source is fetched from and never pushed to, and the
working copy that would let anything write back is never created.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import time
from pathlib import Path, PurePosixPath

import pygit2
from fsspec.implementations.git import GitFileSystem

from ..config import ConfigError, GitSource
from .errors import SourceError

# Written at the root of an export: the commit it holds, then how many files it
# took. Hidden, so harvest.py's dot-file rule keeps it out of every listing by
# itself.
COMMIT_FILE = ".mcp-school-commit"

# A directory under <cache>/src/<name>/ that is not an export -- one being
# built, or one on its way out. Neither prefix can be mistaken for a commit.
WORK_PREFIX = ".tmp-"
DISCARD_PREFIX = ".discard-"

# How long an export the current one superseded is kept beyond the newest of
# them. A request in flight is still reading the tree the pre-swap snapshot was
# built against and nothing down here can know when the last of those finishes,
# so the collection waits out anything that could still be running.
GRACE_SECONDS = 900

SHA = re.compile(r"^[0-9a-f]{40}$")


def materialise_git(source: GitSource, cache: Path) -> Path:
    """Clone or reuse, resolve the ref, export that tree, return the harvest root."""
    repo, fresh = _repository(source, cache)
    commit = _resolve(source, repo, fresh=fresh)
    export = _export(source, cache, repo.path, commit)

    root = export / source.subdirectory if source.subdirectory else export
    if not root.is_dir():
        raise SourceError(
            f"{source.name}: subdirectory {source.subdirectory!r} "
            f"is not a directory at {commit}"
        )
    return root


def fingerprint_git(source: GitSource, cache: Path, root: Path) -> dict:
    """The exported commit and its extent, plus what ``ref`` points at right now.

    Two different commits mean the export is behind the remote, which is
    exactly when ``School.refresh`` should rebuild -- and the same two mean it
    is not, however long ago the clone happened. ``files`` is what makes the
    export's own integrity part of the answer: a tree that lost files since it
    was written has moved as surely as the remote has, and a rebuild is what
    puts it back.

    Read from ``root`` rather than from the cache layout, because an export is
    keyed by its commit: the fingerprint has to describe the tree *this record*
    is served from, not whichever one happens to be newest.
    """
    export = _export_root(source, root)
    exported = _exported(export)
    if exported is None:
        raise SourceError(f"{source.name}: nothing exported under the cache")
    return {
        "commit": exported[0],
        "files": _count(export),
        "ref": source.ref,
        "remote": _tip(source, cache),
    }


def resolve(source: GitSource, cache: Path) -> str:
    """The commit sha ``source.ref`` means right now, fetching it if need be."""
    repo, fresh = _repository(source, cache)
    return _resolve(source, repo, fresh=fresh)


# -- the clone -----------------------------------------------------------------


def _repository(source: GitSource, cache: Path) -> tuple[pygit2.Repository, bool]:
    """The bare clone under the cache, and whether this call is what made it.

    A clone that cannot be opened is thrown away and made again rather than
    failing forever: the only way to get one is an interrupted clone, and the
    cache is derived data that is always safe to rebuild.
    """
    bare = cache / "git" / source.name
    if bare.exists():
        try:
            return pygit2.Repository(str(bare)), False
        except pygit2.GitError:
            shutil.rmtree(bare, ignore_errors=True)

    tmp = bare.with_name(bare.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    bare.parent.mkdir(parents=True, exist_ok=True)
    url = source.clone_url
    try:
        pygit2.clone_repository(
            url,
            str(tmp),
            bare=True,
            depth=_depth(url),
            callbacks=_callbacks(source),
        ).free()
    except pygit2.GitError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SourceError(f"{source.name}: clone failed: {exc}") from exc
    tmp.rename(bare)
    return pygit2.Repository(str(bare)), True


def _depth(url: str) -> int:
    """1 -- a shallow clone is all a source needs -- except where it is refused.

    libgit2's local transport rejects a shallow fetch outright ("shallow fetch
    is not supported by the local transport"), and a local clone has no
    bandwidth to save anyway, so ``git+file://`` is cloned whole. Every remote
    transport gets the single commit it was asked for.
    """
    return 0 if url.startswith("file://") else 1


def _callbacks(source: GitSource) -> pygit2.RemoteCallbacks | None:
    """Credentials for this call only, resolved where they are declared."""
    if source.auth is None:
        return None
    try:
        password = source.auth.password.resolve().get_secret_value()
    except ConfigError as exc:
        raise SourceError(f"{source.name}: {exc}") from exc
    return pygit2.RemoteCallbacks(
        credentials=pygit2.UserPass(source.auth.username, password)
    )


# -- the ref -------------------------------------------------------------------


def _resolve(source: GitSource, repo: pygit2.Repository, *, fresh: bool) -> str:
    """The commit ``source.ref`` names, fetched into ``repo`` if it is not there.

    ``fresh`` says the clone was just made, in which case its HEAD is by
    definition the remote's and asking again would be a second round trip for
    an answer already in hand.
    """
    ref = source.ref
    remote = _remote(source, repo)
    if ref is None:
        if fresh:
            return str(_head(source, repo))
        _fetch(source, remote, ["HEAD"])
        return str(repo.revparse_single("FETCH_HEAD").id)

    if SHA.match(ref):
        if ref not in repo:
            # A shallow clone holds one commit; an older pin is fetched by sha,
            # which is the whole reason the pin is a field and not a URL.
            _fetch(source, remote, [ref])
        if ref not in repo:
            raise SourceError(f"{source.name}: commit {ref} is not in {source.url}")
        return ref

    _fetch(
        source,
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
    raise SourceError(f"{source.name}: ref {ref!r} not found")


def _head(source: GitSource, repo: pygit2.Repository) -> pygit2.Oid:
    try:
        return repo.head.target
    except pygit2.GitError as exc:
        raise SourceError(f"{source.name}: {source.url} has no commits: {exc}") from exc


def _remote(source: GitSource, repo: pygit2.Repository) -> pygit2.Remote:
    try:
        return repo.remotes["origin"]
    except KeyError as exc:
        raise SourceError(f"{source.name}: the clone has no origin remote") from exc


def _fetch(source: GitSource, remote: pygit2.Remote, refspecs: list[str]) -> None:
    depth = _depth(source.clone_url)
    try:
        remote.fetch(refspecs, depth=depth, callbacks=_callbacks(source))
    except pygit2.GitError as exc:
        raise SourceError(f"{source.name}: fetch failed: {exc}") from exc


def _tip(source: GitSource, cache: Path) -> str:
    """What the remote says ``ref`` points at -- one call, and none when pinned."""
    ref = source.ref
    if ref is not None and SHA.match(ref):
        return ref

    wanted = ("HEAD",) if ref is None else (f"refs/heads/{ref}", f"refs/tags/{ref}")
    repo, _ = _repository(source, cache)
    remote = _remote(source, repo)
    try:
        # The callbacks go to list_heads, not to a connect() in front of it:
        # list_heads re-connects unconditionally and with callbacks=None unless
        # it is given some, which silently discards a credentialed handshake
        # made on the line above -- and costs a second round trip to do it.
        heads = {
            head.name: str(head.oid)
            for head in remote.list_heads(callbacks=_callbacks(source))
        }
    except pygit2.GitError as exc:
        raise SourceError(f"{source.name}: cannot reach {source.url}: {exc}") from exc
    for name in wanted:
        if name in heads:
            return heads[name]
    raise SourceError(f"{source.name}: ref {ref!r} not found")


# -- the export ----------------------------------------------------------------


def _export(source: GitSource, cache: Path, bare: str, commit: str) -> Path:
    """``commit``'s tree at ``<cache>/src/<name>/<commit>``, renamed in whole.

    An export already holding this commit is left exactly as it is: a commit is
    its tree, so rewriting it would produce the same bytes. Every other commit
    gets a directory of its own, which is what keeps a refresh from writing
    over the tree a live snapshot is serving -- the record points at the new
    path and the old one is left for ``_collect``.
    """
    home = cache / "src" / source.name
    dest = home / commit
    if _complete(dest, commit):
        _collect(home, dest)
        return dest

    home.mkdir(parents=True, exist_ok=True)
    tmp = home / (WORK_PREFIX + commit)
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        fs = GitFileSystem(path=bare, ref=commit, skip_instance_cache=True)
        fs.get("", str(tmp), recursive=True)
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SourceError(f"{source.name}: export of {commit} failed: {exc}") from exc

    # An empty tree exports no directory at all, and the stamp goes in last and
    # *inside* tmp: the rename that publishes the export is the only thing that
    # creates <commit>/, so the directory never exists half built.
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / COMMIT_FILE).write_text(f"{commit}\n{_count(tmp)}\n", encoding="utf-8")
    if dest.exists():
        # Incomplete, or the short-circuit above would have taken it. Nothing
        # is serving it, and rename refuses a destination that is there.
        _discard(dest)
    tmp.rename(dest)
    _collect(home, dest)
    return dest


def _export_root(source: GitSource, root: Path) -> Path:
    """The export ``root`` lies in -- itself, unless ``subdirectory`` moved it."""
    for _ in PurePosixPath(source.subdirectory or "").parts:
        root = root.parent
    return root


def _complete(dest: Path, commit: str) -> bool:
    """Whether ``dest`` is an export of ``commit`` with all of it still present.

    Two facts, deliberately: the stamp names the commit, and the tree still
    holds the number of files that export wrote. A stamp on its own only proves
    that an export once started here. An interrupted delete leaves one behind,
    and reusing that tree is how a source goes on reporting ``ok`` while
    serving nothing at all.
    """
    exported = _exported(dest)
    return exported is not None and exported == (commit, _count(dest))


def _count(dest: Path) -> int:
    """Regular files in the export at ``dest``, its own stamp apart."""
    stamp = dest / COMMIT_FILE
    return sum(1 for p in dest.rglob("*") if p != stamp and p.is_file())


def _exported(dest: Path) -> tuple[str, int] | None:
    """The commit and file count the export at ``dest`` claims, or None."""
    try:
        commit, _, count = (
            (dest / COMMIT_FILE).read_text(encoding="utf-8").partition("\n")
        )
        return commit.strip(), int(count)
    except (OSError, ValueError):
        return None


def _collect(home: Path, keep: Path) -> None:
    """Drop the exports and work trees nothing can still be reading.

    ``keep`` is the export just made current. The newest of the others is kept
    unconditionally -- it is the one the live snapshot was built against, and
    it stays whatever its age, because a source that has not moved in a month
    is still being served from the tree it exported a month ago. The rest go
    once ``GRACE_SECONDS`` have passed, which bounds the cache at the commits a
    source moved through recently instead of every commit it ever saw.

    Best effort: a cache that cannot be tidied is not a reason to fail a build.
    """
    with contextlib.suppress(OSError):
        exports = sorted(
            (p for p in home.iterdir() if p != keep and SHA.match(p.name)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        cutoff = time.time() - GRACE_SECONDS
        doomed = [p for p in exports[1:] if p.stat().st_mtime < cutoff]
        doomed += [
            p
            for p in home.iterdir()
            if p.name.startswith((WORK_PREFIX, DISCARD_PREFIX))
        ]
        for path in doomed:
            _discard(path)


def _discard(path: Path) -> None:
    """Delete ``path``, taking it out of the commit-named space first.

    A rename is atomic where an ``rmtree`` is not. Interrupt the delete and
    what is left is a ``.discard-`` directory nothing will ever read again,
    rather than a truncated tree sitting at the name of a commit some later
    export would otherwise trust.
    """
    if path.name.startswith(DISCARD_PREFIX):
        shutil.rmtree(path, ignore_errors=True)
        return
    grave = path.with_name(DISCARD_PREFIX + path.name)
    shutil.rmtree(grave, ignore_errors=True)
    with contextlib.suppress(OSError):
        path.rename(grave)
        shutil.rmtree(grave, ignore_errors=True)
