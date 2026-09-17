"""Refusing a scope that names nothing, rather than serving it an empty catalogue.

A client's scope is set once, by whoever configures it: ``?library=grafana`` on
its MCP URL, or an ``X-Skill-Library`` header. A typo there used to be served
exactly as asked -- a catalogue with nothing in it, no error, nothing in any
log -- which from the client looks like a server that has no skills. So a scope
that names a library no source joins, a folder no skill of that library sits
under, or a tag nothing carries, fails every request with an error that says
what is there instead, and a client that cannot connect says why.

The error names libraries, folders and tags. That is not a leak: a scope is the
operator's choice of what a client is given, not a boundary the model is kept
behind, and the operator reading this error is the one who wrote the config.

A scope is refused for what it *names*, never for what happens to be served at
the moment. A library whose every source is down right now, or up with nothing
in it yet, is still a library; it is empty, and a client pinned to it connects
and sees nothing until something arrives, which ``/health`` explains. Only a
folder or a tag combination is checked against the served catalogue, and only
when that library is serving.
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


def what_is_wrong(scope: Scope, config: Config, snapshot: Snapshot) -> str | None:
    """Why ``scope`` names nothing in this catalogue, or None if it names something."""
    if not scope:
        return None
    libraries = sorted(
        {lib.name for lib in config.libraries}
        | {source.library_name for source in config.sources}
    )
    if scope.library and scope.library_name not in libraries:
        return (
            f"The scope names library {scope.library_name!r}, and there is no"
            f" such library. The libraries are: {', '.join(libraries)}."
        )

    tags = _declared_tags(config)
    unknown = sorted(scope.tags - tags)
    if unknown:
        return (
            f"The scope names tag{'s' if len(unknown) > 1 else ''}"
            f" {', '.join(map(repr, unknown))}, which nothing carries. The tags"
            f" are: {', '.join(sorted(tags))}."
        )

    # Checked against what the scope's own tags admit, so the answer never
    # confirms a skill the scope could not see.
    tagged = Scope(library=scope.library_name, tags=scope.tags)
    in_library = snapshot.index.visible(tagged)
    if scope.folder and _up(snapshot, scope.library_name):
        address = scope.library
        if snapshot.index.get(address, tagged) is not None:
            return (
                f"The scope names {address!r}, which is a skill, not a folder."
                " A scope is a library or a folder of one."
            )
        if not any(
            s.folder == scope.folder or s.folder.startswith(f"{scope.folder}/")
            for s in in_library
        ):
            folders = sorted({s.folder.split("/")[0] for s in in_library} - {""})
            return (
                f"The scope names folder {scope.folder!r} of library"
                f" {scope.library_name!r}, which has no such folder."
                + (
                    f" Its folders are: {', '.join(folders)}."
                    if folders
                    else " It has no folders."
                )
            )

    if scope.library and not _up(snapshot, scope.library_name):
        # Named correctly and serving nothing right now: see the module note.
        return None
    if not scope.library and not _serving(snapshot, EVERYTHING):
        return None
    if scope.tags and not _serving(snapshot, scope):
        where = f"library {scope.library!r}" if scope.library else "any library"
        return (
            f"The scope names tags that exist, but nothing in {where} carries"
            f" any of them: {', '.join(sorted(scope.tags))}."
        )
    return None


def _up(snapshot: Snapshot, library: str) -> bool:
    """Whether any source of ``library`` is serving, however little.

    Not whether it serves skills: a library of prompts or files is up and has
    no folders, and a folder scope on it names nothing.
    """
    return any(
        entry.get("library") == library and entry.get("status") in ("ok", "stale")
        for entry in snapshot.status.values()
    )


def _declared_tags(config: Config) -> set[str]:
    """Every tag anything can carry, whether or not it is serving.

    Which is every tag there is: a skill's and a prompt's tags are made from the
    config alone -- its library and source names, ``skill`` or ``prompt``, and
    the tags the library and source declare -- never from a file. So this needs
    no walk of the catalogue, which matters on a check made for every request.
    """
    tags = {"skill", "prompt"}
    for lib in config.libraries:
        tags |= {lib.name, *lib.tags}
    for source in config.sources:
        tags |= {source.name, source.library_name, *source.tags}
    return tags


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
