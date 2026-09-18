"""``file://`` -- a directory on this machine, served where it is."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..catalogue.harvest import FINGERPRINTED_DOTDIRS, inside
from ..plugins import Fetch
from .errors import SourceError


def materialise_file(fetch: Fetch, cache: Path) -> Path:
    """The directory the fetch names, as it is: a ``file://`` URL is a path."""
    path = Path(fetch.url)
    if not path.is_dir():
        raise SourceError(f"{fetch.key}: {path} is not a directory")
    return path


def fingerprint_file(fetch: Fetch, cache: Path, root: Path) -> dict:
    """A cheap summary of ``root``: file count, total bytes, newest mtime.

    Served in place, so there is no clone or cache state to key off --
    ``fetch`` and ``cache`` are accepted only to keep the same signature every
    backend's fingerprint shares. Walked with ``os.walk`` (no globs), skipping
    hidden directories except ``harvest.FINGERPRINTED_DOTDIRS``: the ones a
    harvest reads, *plus* ``.claude-plugin``, which holds the marketplace and
    manifest files a harvest is steered by. Editing a local
    ``marketplace.json`` is the whole point of a ``file://`` marketplace, and
    it changes nothing else in the tree -- so a walk that could not see it left
    the fingerprint where it was and a configured ``refresh:`` never re-read
    the catalogue. Those directories stay ``harvest.hidden``, so nothing in
    them is ever served.

    Only regular files count, and a symlink counts only when it lands *inside*
    the root -- the same rule ``harvest.files`` applies, and the two must agree.
    Following one that escapes would fold a file elsewhere on the disk, its size
    and its mtime, into this fetch's fingerprint, so an unrelated edit would
    rebuild this fetch; refusing them all instead makes a Kubernetes ConfigMap
    mount, which is *entirely* symlinks, fingerprint as empty and therefore
    never look changed however often it is updated. Directory links follow the
    same rule, since a volume ``items[].path`` such as ``shared/foo.md`` mounts
    as ``shared -> ..data/shared``; each resolved directory is walked once.
    """
    del fetch, cache
    base = root.resolve()
    files = 0
    total_bytes = 0
    newest = 0
    # Resolved directories already walked. Following in-root directory links is
    # what reaches a mount like `shared -> ..data/shared`; this set is what stops
    # a link back up the tree from walking forever.
    seen = {base}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        kept = []
        for d in dirnames:
            if d.startswith(".") and d not in FINGERPRINTED_DOTDIRS:
                continue
            target = inside(Path(dirpath) / d, base)
            if target is None or target in seen:
                continue
            seen.add(target)
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            path = Path(dirpath) / name
            try:
                st = os.lstat(path)
                if stat.S_ISLNK(st.st_mode):
                    target = inside(path, base)
                    if target is None:
                        continue
                    st = target.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files += 1
            total_bytes += st.st_size
            newest = max(newest, st.st_mtime_ns)
    return {"files": files, "bytes": total_bytes, "newest": newest}
