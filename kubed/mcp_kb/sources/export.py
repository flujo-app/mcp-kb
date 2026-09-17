"""Materialising a tree into the cache without disturbing the one being served.

Every backend that copies a source into ``<cache>/src/<name>`` has the same
problem, and it is not about git or about WebDAV: a refresh happens while the
pre-swap snapshot is still serving the tree the refresh is replacing.
``rmtree(dest)`` then ``rename(tmp, dest)`` is the obvious way to do it, and it
corrupted 56 of 222 concurrent reads on one ordinary refresh; worse, a crash
inside that window leaves a half tree still carrying its completion stamp,
which the next start trusts and serves nothing out of while reporting ``ok``.

So a tree is never rewritten in place. Each *version* of a source gets a
directory of its own under the source's home, and the backend says what a
version is: for git it is the commit, for WebDAV the digest of the folder's
ETags. From there the mechanism is identical, and the four rules it keeps are:

- **Built aside, published by a rename onto a free name.** The work happens
  under a ``.tmp-`` sibling and the rename is the only thing that creates the
  export, so the directory never exists half built. Where a version's own name
  is already taken -- by an export of it that lost files, which is the one case
  a rebuild is *for* -- the new tree goes to ``<version>.1`` rather than over
  the top: that name is the one a live snapshot is serving from.
- **The stamp goes in last, and carries a count.** A stamp alone only says an
  export once started here -- an interrupted delete leaves one behind. The
  number of files that export wrote is what distinguishes the tree that is
  whole from the tree that is a name.
- **A delete is a rename first.** ``.discard-`` takes a tree out of the
  version-named space in one atomic step, so an interrupted ``rmtree`` cannot
  leave a truncated tree at a name a later export would trust.
- **A file written into an export is staged outside it.** ``.work/`` beside
  the exports is where a live read's download lands before it is renamed into
  place. Inside the tree it would count towards the export's extent -- so a
  temp file left by a killed process would make a whole export look truncated
  for ever -- and the collector, which walks the source's home, could never
  reach it.
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

# Where a file being written *into* an export is staged. A sibling of the
# exports rather than a child of one, and named so that neither the version
# pattern nor the leftover prefixes claim it: the collector empties it of stale
# files, it never deletes the directory a fetch in flight is using.
WORK_DIR = ".work"

# What ``ensure`` appends to a version whose own name is taken. Neither a
# commit nor a digest carries a dot, so stripping this off a directory's name
# leaves the version it holds and nothing else.
GENERATION = re.compile(r"\.\d+$")

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

    @classmethod
    def under(
        cls, cache: Path, name: str, *, stamp: str, version: re.Pattern[str]
    ) -> Exports:
        """``name``'s export space under ``cache``: one home, a directory per version.

        The layout is the same whichever backend fills it, so it is spelled
        here rather than once per backend that copies a tree into the cache.
        """
        return cls(name=name, home=cache / "src" / name, stamp=stamp, version=version)

    def ensure(self, version: str, build: Callable[[Path], None]) -> Path:
        """``version``'s tree under ``home``, built by ``build`` if there is none.

        An export already holding this version whole is left exactly as it is:
        a version is its tree, so rewriting it would produce the same bytes and
        the only thing the rewrite could change is what a reader is part way
        through reading.

        One that holds it in *part* is the interesting case, and it is why the
        destination is whatever name is free rather than the version's own. A
        rebuild of the same version happens when an export lost files -- and
        the tree that lost them is the tree the live snapshot is serving out
        of, since an export is named by its version and the version has not
        moved. Publishing over it would mean deleting it first. So the repair
        goes to ``<version>.1`` like any other new export, and the one that is
        being read is retired by ``collect`` once the grace period is up.
        """
        current = self.current(version)
        if current is not None:
            self.collect(current)
            return current

        self.home.mkdir(parents=True, exist_ok=True)
        dest = self.free(version)
        tmp = self.home / (WORK_PREFIX + dest.name)
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
        # The version, not the directory's name: a repair published at
        # <version>.1 still holds <version>, and that is what the next pass
        # compares against.
        (tmp / self.stamp).write_text(
            f"{version}\n{self.count(tmp)}\n", encoding="utf-8"
        )
        # dest was free when it was chosen and nothing else allocates in this
        # home, so the rename creates it rather than replacing anything.
        tmp.rename(dest)
        self.collect(dest)
        return dest

    def current(self, version: str) -> Path | None:
        """The export holding all of ``version``, whichever of its names it is at.

        Newest generation first: a repair is the one to serve, and the tree it
        replaced is the one ``collect`` retires.
        """
        for dest in reversed(self.generations(version)):
            if self.complete(dest, version):
                return dest
        return None

    def generations(self, version: str) -> list[Path]:
        """Every directory that could hold ``version``: its name, then its retries."""
        try:
            names = [p.name for p in self.home.iterdir() if p.is_dir()]
        except OSError:
            return []
        held = sorted((n for n in names if base(n) == version), key=_generation)
        return [self.home / n for n in held]

    def free(self, version: str) -> Path:
        """The first name in ``version``'s series that nothing holds."""
        dest = self.home / version
        generation = 0
        while dest.exists():
            generation += 1
            dest = self.home / f"{version}.{generation}"
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
                (p for p in others if self.version.match(base(p.name))),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            cutoff = time.time() - GRACE_SECONDS
            doomed = [p for p in exports[1:] if p.stat().st_mtime < cutoff]
            leftovers = (WORK_PREFIX, DISCARD_PREFIX)
            doomed += [p for p in others if p.name.startswith(leftovers)]
            for path in doomed:
                discard(path)
            # .work survives -- a fetch may be writing in it right now -- but
            # what a killed one left behind does not.
            for path in (self.home / WORK_DIR).glob("*"):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)


def base(name: str) -> str:
    """An export directory's version: its name, any generation suffix stripped."""
    return GENERATION.sub("", name)


def _generation(name: str) -> int:
    """Which retry ``name`` is -- 0 for the version's own name."""
    suffix = GENERATION.search(name)
    return int(suffix.group()[1:]) if suffix else 0


def workspace(home: Path) -> Path:
    """``.work/`` beside ``home``'s exports, made if it is not there yet.

    Where a file bound for an export is written before it is renamed into one.
    Same filesystem as the export, so the rename is still atomic; outside it,
    so a temp file no process is left to clean up cannot be mistaken for part
    of the export's extent.
    """
    work = home / WORK_DIR
    work.mkdir(parents=True, exist_ok=True)
    return work


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
