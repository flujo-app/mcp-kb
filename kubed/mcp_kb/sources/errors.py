"""``SourceError`` lives on its own so ``file.py`` need not import the package
``__init__`` that imports ``file.py`` -- a straight import, not a circular one
resolved by deferring it to call time.
"""

from __future__ import annotations


class SourceError(RuntimeError):
    """A source could not be materialised."""
