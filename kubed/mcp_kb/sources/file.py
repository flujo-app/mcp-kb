"""``file://`` -- a directory on this machine, served where it is."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..catalogue.harvest import CONVENTIONAL_DOTDIRS, inside
from ..config import FileSource
from .errors import SourceError


def materialise_file(source: FileSource, cache: Path) -> Path:
    if not source.path.is_dir():
        raise SourceError(f"{source.name}: {source.path} is not a directory")
    return source.path


def fingerprint_file(source: FileSource, cache: Path, root: Path) -> dict:
    """A cheap summary of ``root``: file count, total bytes, newest mtime.

    Served in place, so there is no clone or cache state to key off -- ``cache``
    is accepted only to keep the same signature every backend's fingerprint
    shares. Walked with ``os.walk`` (no globs), skipping hidden directories
    except the conventional agent-tooling ones, same as ``harvest.files``.

    Only regular files count, and a symlink counts only when it lands *inside*
    the root -- the same rule ``harvest.files`` applies, and the two must agree.
    Following one that escapes would fold a file elsewhere on the disk, its size
    and its mtime, into this source's fingerprint, so an unrelated edit would
    rebuild this source; refusing them all instead makes a Kubernetes ConfigMap
    mount, which is *entirely* symlinks, fingerprint as empty and therefore
    never look changed however often it is updated. Directory links follow the
    same rule, since a volume ``items[].path`` such as ``shared/foo.md`` mounts
    as ``shared -> ..data/shared``; each resolved directory is walked once.
    """
    del source, cache
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
            if d.startswith(".") and d not in CONVENTIONAL_DOTDIRS:
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
