"""``file://`` -- a directory on this machine, served where it is."""

from __future__ import annotations

from pathlib import Path

from ..config import FileSource
from .errors import SourceError


def materialise_file(source: FileSource, cache: Path) -> Path:
    if not source.path.is_dir():
        raise SourceError(f"{source.name}: {source.path} is not a directory")
    return source.path
