"""Refusing a scope that names nothing, rather than serving it an empty catalogue.

A client's scope is set once, by whoever configures it: ``?library=grafana`` on
its MCP URL, or an ``X-Skill-Library`` header. A typo there used to be served
exactly as asked -- a catalogue with nothing in it, no error, nothing in any
log -- which from the client looks like a server that has no skills. So a scope
that names a library the config does not declare, a category no plugin has, or
a tag nothing carries, fails every request with an error that says what is
there instead, and a client that cannot connect says why.

The error names libraries, categories and tags. That is not a leak: a scope is
the operator's choice of what a client is given, not a boundary the model is
kept behind, and the operator reading this error is the one who wrote the
config.

A scope is refused for what it *names*, never for what happens to be served at
the moment. A library whose every plugin is down right now, or up with nothing
in it yet, is still a library; it is empty, and a client pinned to it connects
and sees nothing until something arrives, which ``/health`` explains. Only a
category or a tag combination is checked against the served catalogue, and
only when that library is serving.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastmcp.server.middleware import Middleware
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from .prompts import PromptProvider
from .request import requested_scope
from .scope import EVERYTHING, Scope

if TYPE_CHECKING:
    from ..catalogue.snapshot import Snapshot
    from ..config import Config

FOLDER_IN_LIBRARY = (
    "A scope names a library, not a folder: use ?categories= or ?tags= to narrow"
    " inside it."
)

COMMA_IN_CATEGORY = (
    "A plugin has one category, so 'a,b' matches nothing; repeat ?categories= to"
    " mean any of them."
)


def what_is_wrong(scope: Scope, config: Config, snapshot: Snapshot) -> str | None:
    """Why ``scope`` names nothing in this catalogue, or None if it names something."""
    if not scope:
        return None
    if "/" in scope.library:
        return FOLDER_IN_LIBRARY
    if scope.library and config.library(scope.library) is None:
        libraries = ", ".join(sorted(lib.name for lib in config.libraries))
        return (
            f"The scope names library {scope.library!r}, and there is no"
            f" such library. The libraries are: {libraries}."
        )
    if any("," in category for category in scope.categories):
        return COMMA_IN_CATEGORY

    # What the plugins declare, in the library if one is named and anywhere
    # otherwise -- from the snapshot, since a marketplace's plugins are not in
    # the config to be read from.
    plugins = [
        entry
        for entry in snapshot.status.get("plugins", {}).values()
        if not scope.library or scope.library in entry.get("libraries", [])
    ]
    categories = {e["category"] for e in plugins if e.get("category")}
    unknown = sorted(scope.categories - categories)
    if unknown:
        return (
            f"The scope names categor{'ies' if len(unknown) > 1 else 'y'}"
            f" {', '.join(map(repr, unknown))}, which no plugin declares. The"
            f" categories are: {_listed(categories)}."
        )
    tags = {t for e in plugins for t in (*e.get("tags", ()), *e.get("keywords", ()))}
    unknown = sorted({tag for group in scope.tags for tag in group} - tags)
    if unknown:
        return (
            f"The scope names tag{'s' if len(unknown) > 1 else ''}"
            f" {', '.join(map(repr, unknown))}, which nothing carries. The tags"
            f" are: {_listed(tags)}."
        )

    if scope.library and not _up(snapshot, scope.library):
        # Named correctly and serving nothing right now: see the module note.
        return None
    if not scope.library and not _serving(snapshot, EVERYTHING):
        return None
    if (scope.categories or scope.tags) and not _serving(snapshot, scope):
        where = f"library {scope.library!r}" if scope.library else "any library"
        return (
            f"The scope names things that exist, but nothing in {where} carries"
            f" the combination: {_asked(scope)}."
        )
    return None


def _listed(names: set[str]) -> str:
    return ", ".join(sorted(names)) or "none"


def _asked(scope: Scope) -> str:
    """The scope's categories and tag groups, as the request spelt them."""
    parts = [f"category {c}" for c in sorted(scope.categories)]
    parts += [
        f"tags {','.join(sorted(group))}" for group in sorted(scope.tags, key=sorted)
    ]
    return "; ".join(parts)


def _up(snapshot: Snapshot, library: str) -> bool:
    """Whether any plugin of ``library`` is serving, however little.

    Not whether it serves skills: a library of prompts or files is up, and a
    category scope on it can still name nothing.
    """
    entry = snapshot.status.get("libraries", {}).get(library, {})
    plugins = snapshot.status.get("plugins", {})
    return any(
        plugins.get(pid, {}).get("status") == "ok" for pid in entry.get("plugins", ())
    )


def _serving(snapshot: Snapshot, scope: Scope) -> bool:
    """Whether ``scope`` would be shown anything at all: a row, or a prompt."""
    if snapshot.catalogue.entries(scope):
        return True
    return bool(PromptProvider(lambda: snapshot).visible(scope))


class RefuseEmptyScope(Middleware):
    """Fail every request whose scope names nothing, with what there is instead.

    Every request, not only a listing, so that the first one a client makes --
    which for most is the one it connects with -- is the one that fails.
    Invalid params, -32602: the request is well formed, and what it asks for is
    not in this catalogue.
    """

    def __init__(self, problem: Callable[[Scope], str | None]):
        self._problem = problem

    async def on_request(self, context, call_next):
        scope = requested_scope()
        if scope:
            problem = self._problem(scope)
            if problem is not None:
                raise MCPError(code=INVALID_PARAMS, message=problem)
        return await call_next(context)
