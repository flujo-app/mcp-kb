"""The OpenAPI document for the HTTP surface.

Re-exported here so a caller says ``from .spec import build_spec`` without
caring that it lives in ``builder.py`` -- the split exists so a future second
document (there is only one endpoint pair today) has somewhere to grow that
is not this file.
"""

from __future__ import annotations

from .builder import DIST_NAME, PLACEHOLDER_VERSION, TITLE, build_spec

__all__ = ["DIST_NAME", "PLACEHOLDER_VERSION", "TITLE", "build_spec"]
