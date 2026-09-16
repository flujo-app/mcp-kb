"""Materialising a tree into the cache without disturbing the one being served.

Every backend that copies a source into ``<cache>/src/<name>`` has the same
problem, and it is not about git or about WebDAV: a refresh happens while the
pre-swap snapshot is still serving the tree the refresh is replacing. E3 shipped
``rmtree(dest)`` then ``rename(tmp, dest)`` and it corrupted 56 of 222
concurrent reads on one ordinary refresh; worse, a crash inside that window left
a half tree still carrying its completion stamp, which the next start trusted
and served nothing out of while reporting ``ok``.

So a tree is never rewritten in place. Each *version* of a source gets a
directory of its own under the source's home, and the backend says what a
version is: for git it is the commit, for WebDAV the digest of the folder's
ETags. From there the mechanism is identical, and the four rules it keeps are:

- **Built aside, published by a rename.** The work happens under a
  ``.tmp-<version>`` sibling and the rename is the only thing that creates
  ``<version>/``, so the directory never exists half built.
- **The stamp goes in last, and carries a count.** A stamp alone only says an
  export once started here -- an interrupted delete leaves one behind. The
  number of files that export wrote is what distinguishes the tree that is
  whole from the tree that is a name.
- **A delete is a rename first.** ``.discard-`` takes a tree out of the
  version-named space in one atomic step, so an interrupted ``rmtree`` cannot
  leave a truncated tree at a name a later export would trust.
- **The newest superseded export is kept whatever its age.** It is the one the
  live snapshot was built against, and a source that has not moved in a month
  is still being served from the tree it exported a month ago. The rest go once
  the grace period has passed.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import SourceError

# A directory under a source's home that is not an export -- one being built,
# or one on its way out. Neither prefix can be mistaken for a version.
WORK_PREFIX = ".tmp-"
DISCARD_PREFIX = ".discard-"

# How long an export the current one superseded is kept beyond the newest of
# them. A request in flight is still reading the tree the pre-swap snapshot was
# built against and nothing down here can know when the last of those finishes,
# so the collection waits out anything that could still be running.
GRACE_SECONDS = 900


@dataclass(frozen=True)
class Exports:
    """One source's per-version export space under the cache.

    ``stamp`` is the completion file's name, hidden so harvest.py's dot-file
    rule keeps it out of every listing by itself, and ``version`` is what a
    version directory's name looks like -- the pattern that tells an export
    from anything else that ended up in the home directory.
    """

    name: str
    home: Path
    stamp: str
    version: re.Pattern[str]

    def ensure(self, version: str, build: Callable[[Path], None]) -> Path:
        """``version``'s tree at ``<home>/<version>``, built by ``build`` if new.

        An export already holding this version is left exactly as it is: a
        version is its tree, so rewriting it would produce the same bytes and
        the only thing the rewrite could change is what a reader is part way
        through reading.
        """
        dest = self.home / version
        if self.complete(dest, version):
            self.collect(dest)
            return dest

        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.home / (WORK_PREFIX + version)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            build(tmp)
        except SourceError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SourceError(
                f"{self.name}: export of {version} failed: {exc}"
            ) from exc

        # An empty source writes no directory at all, and the stamp goes in
        # last and *inside* tmp: the rename that publishes the export is the
        # only thing that creates <version>/.
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / self.stamp).write_text(
            f"{version}\n{self.count(tmp)}\n", encoding="utf-8"
        )
        if dest.exists():
            # Incomplete, or the short-circuit above would have taken it.
            # Nothing is serving it, and rename refuses a destination that is
            # already there.
            discard(dest)
        tmp.rename(dest)
        self.collect(dest)
        return dest

    def complete(self, dest: Path, version: str) -> bool:
        """Whether ``dest`` is an export of ``version`` with all of it present."""
        return self.exported(dest) == (version, self.count(dest))

    def exported(self, dest: Path) -> tuple[str, int] | None:
        """The version and file count the export at ``dest`` claims, or None."""
        try:
            version, _, count = (
                (dest / self.stamp).read_text(encoding="utf-8").partition("\n")
            )
            return version.strip(), int(count)
        except (OSError, ValueError):
            return None

    def count(self, dest: Path) -> int:
        """Regular files in the export at ``dest``, its own stamp apart."""
        stamp = dest / self.stamp
        return sum(1 for p in dest.rglob("*") if p != stamp and p.is_file())

    def collect(self, keep: Path) -> None:
        """Drop the exports and work trees nothing can still be reading.

        ``keep`` is the export just made current; the newest of the others
        stays whatever its age, and the rest go once ``GRACE_SECONDS`` have
        passed. Best effort: a cache that cannot be tidied is not a reason to
        fail a build.
        """
        with contextlib.suppress(OSError):
            others = [p for p in self.home.iterdir() if p.is_dir() and p != keep]
            exports = sorted(
                (p for p in others if self.version.match(p.name)),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            cutoff = time.time() - GRACE_SECONDS
            doomed = [p for p in exports[1:] if p.stat().st_mtime < cutoff]
            leftovers = (WORK_PREFIX, DISCARD_PREFIX)
            doomed += [p for p in others if p.name.startswith(leftovers)]
            for path in doomed:
                discard(path)


def discard(path: Path) -> None:
    """Delete ``path``, taking it out of the version-named space first.

    A rename is atomic where an ``rmtree`` is not. Interrupt the delete and
    what is left is a ``.discard-`` directory nothing will ever read again,
    rather than a truncated tree sitting at the name of a version some later
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
