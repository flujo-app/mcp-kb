"""The catalogue, served as MCP resources.

MCP already has a primitive for "material an agent reads", and it is the
resource, not the tool. So this is the real interface and ``tools.py`` is a
mirror of it, kept for the clients -- n8n above all -- to which a resource-only
server looks empty.

One provider serves the whole address space rather than one provider per skill.
FastMCP's ``SkillsDirectoryProvider`` is the obvious alternative, and it is
wrong here for two reasons that only show up at scale:

- It names a skill after its folder, so two libraries shipping a ``testing/``
  become one, and the loser is not merely shadowed but absent from the
  server. With four libraries and ninety skills that is a matter of time,
  not luck.
- It lists every skill and every manifest on every ``resources/list`` -- 77KB
  for this catalogue, paid by every client on every listing. That is the same
  expense this server rejects ``ResourcesAsTools`` for.

``Catalogue`` fixes both by listing *indexes* and resolving content on demand,
which is the resource half of progressive disclosure. It would be cleaner still
as a directory tree, but MCP's ``resources/directory/read`` has no
implementation in FastMCP 4.0.3, so a dozen index URIs are the closest thing
available.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from fastmcp.resources import TextResource
from fastmcp.resources.base import Resource
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.base import Provider
from fastmcp.utilities.versions import VersionSpec
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from ..catalogue.uris import Catalogue
from .request import full_listing, requested_scope


class CatalogueProvider(Provider):
    """Every ``skill://`` URI, scoped to whoever is asking.

    The scope is applied here rather than in middleware because it is applied on
    *every* call anyway -- ``Catalogue`` has no method that does not take
    the scope. Middleware would be a second place for it to be forgotten.

    An out-of-scope URI resolves to None, which FastMCP reports as an unknown
    resource. That conflation is deliberate and matches ``SkillIndex``: a scoped
    client must not be able to confirm another library's contents from the shape
    of an error.

    The catalogue arrives as a getter, not a value. A refresh builds a new one
    and swaps the server's snapshot; a provider holding the old object would
    serve the generation it was registered in for the life of the process.
    """

    def __init__(self, catalogue: Callable[[], Catalogue]):
        super().__init__()
        self._catalogue = catalogue

    async def _list_resources(self) -> Sequence[Resource]:
        entries = self._catalogue().entries(requested_scope(), full=full_listing())
        return [
            TextResource(
                uri=entry.uri,
                name=entry.name,
                description=entry.description,
                mime_type=entry.mime_type,
                tags=set(entry.tags),
                # Listing rows are addresses, not content. The body is fetched
                # when the URI is actually read; putting it here would read the
                # whole catalogue off disk to answer "what is there?".
                text="",
            )
            for entry in entries
        ]

    async def _get_resource(
        self, uri: str, version: VersionSpec | None = None
    ) -> Resource | None:
        catalogue = self._catalogue()
        body = catalogue.read(uri, requested_scope())
        if body is None:
            return None
        return TextResource(
            uri=uri,
            name=uri.removeprefix("skill://"),
            mime_type=catalogue.mime(uri),
            text=body,
        )


class NameTheFileToRead(Middleware):
    """Turn a read of a directory address into an error that says what to read.

    A library, a folder and a skill's root are directories, and a directory
    serves nothing. The provider answers None for them like any other missing
    URI, and FastMCP reports ``Resource not found`` -- true, and no help to an
    agent that learned the shorthand elsewhere. This rewrites that one error to
    name the ``_index.md``, ``SKILL.md`` or ``_manifest`` to read instead.

    The hint is answered from the caller's own scope, so it never confirms a
    directory the caller could not have listed: out of scope, the error is the
    same bare one a missing URI gets. The code is unchanged, -32602.
    """

    def __init__(self, catalogue: Callable[[], Catalogue]):
        self._catalogue = catalogue

    async def on_read_resource(self, context, call_next):
        try:
            return await call_next(context)
        except NotFoundError:
            uri = str(context.message.uri)
            hint = self._catalogue().directory(uri, requested_scope())
            if hint is None:
                raise
            raise MCPError(
                code=INVALID_PARAMS,
                message=f"Resource not found: {uri!r}. {hint}",
                data={"uri": uri},
            ) from None


class HideMirrorTools(Middleware):
    """Drop the mirroring tools from ``tools/list`` for clients that have the
    native feature each one mirrors.

    Every tool this server has is a mirror: ``list_resources``/``read_resource``
    stand in for MCP resources and ``list_prompts``/``get_prompt`` for MCP
    prompts, for clients that cannot use those. Advertising both shapes to a
    client that has the real one is noise -- two ways to ask one question, and
    the model has to pick one.

    Filtering the listing rather than registering conditionally is what keeps
    one server object correct for every client at once. The decision depends on
    who is asking, so it can only be made per request; registration happens once
    at boot, when there is no asker.

    They stay *callable* either way. Hiding a tool from a listing is a
    presentation choice; refusing to run one a client already knows about would
    be a different and worse contract.
    """

    def __init__(self, mirrors: dict[str, Callable[[], bool]]):
        # tool name -> "does this client have the native feature it mirrors?"
        self.mirrors = dict(mirrors)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [
            tool
            for tool in tools
            if tool.name not in self.mirrors or not self.mirrors[tool.name]()
        ]


def register(mcp: FastMCP, catalogue: Callable[[], Catalogue]) -> None:
    """Publish the catalogue as ``skill://`` resources, read per request."""
    mcp.add_provider(CatalogueProvider(catalogue))
    mcp.add_middleware(NameTheFileToRead(catalogue))
