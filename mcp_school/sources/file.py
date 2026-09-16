"""``file://`` -- a directory on this machine, served where it is."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..config import FileSource
from ..harvest import CONVENTIONAL_DOTDIRS
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

    Only regular files count, and ``os.lstat`` is what decides: a ``stat`` would
    resolve a symlink and fold a file outside the tree -- its size, its mtime --
    into this source's fingerprint, so an unrelated edit elsewhere on the disk
    would trigger a rebuild here. ``os.walk`` already declines to descend a
    symlinked directory for the same reason.
    """
    del source, cache
    files = 0
    total_bytes = 0
    newest = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") or d in CONVENTIONAL_DOTDIRS
        ]
        for name in filenames:
            try:
                st = os.lstat(Path(dirpath) / name)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files += 1
            total_bytes += st.st_size
            newest = max(newest, st.st_mtime_ns)
    return {"files": files, "bytes": total_bytes, "newest": newest}
