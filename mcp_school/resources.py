"""The catalogue, served as MCP resources.

MCP already has a primitive for "material an agent reads", and it is the
resource, not the tool. So this is the real interface and ``tools.py`` is a
mirror of it, kept for the clients -- n8n above all -- to which a resource-only
server looks empty.

One provider serves the whole address space rather than one provider per skill.
FastMCP's ``SkillsDirectoryProvider`` is the obvious alternative and was what
this server used; it is wrong here for two reasons that only show up at scale:

- It names a skill after its folder, so two packs shipping a ``testing/`` become
  one, and the loser is not merely shadowed but absent from the server. With
  four packs and ninety skills that is a matter of time, not luck.
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

from collections.abc import Sequence

from fastmcp import FastMCP
from fastmcp.resources import TextResource
from fastmcp.resources.base import Resource
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.base import Provider
from fastmcp.utilities.versions import VersionSpec

from .request import client_reads_resources, full_listing, requested_pack
from .uris import Catalogue


class CatalogueProvider(Provider):
    """Every ``skill://`` URI, scoped to whoever is asking.

    The scope is applied here rather than in middleware because it is applied on
    *every* call anyway -- ``Catalogue`` has no method that does not take
    ``pinned``. Middleware would be a second place for it to be forgotten.

    An out-of-scope URI resolves to None, which FastMCP reports as an unknown
    resource. That conflation is deliberate and matches ``SkillIndex``: a pinned
    client must not be able to confirm another pack's contents from the shape of
    an error.
    """

    def __init__(self, catalogue: Catalogue):
        super().__init__()
        self._catalogue = catalogue

    async def _list_resources(self) -> Sequence[Resource]:
        entries = self._catalogue.entries(requested_pack(), full=full_listing())
        return [
            TextResource(
                uri=entry.uri,
                name=entry.name,
                description=entry.description,
                mime_type=entry.mime_type,
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
        body = self._catalogue.read(uri, requested_pack())
        if body is None:
            return None
        return TextResource(
            uri=uri,
            name=uri.removeprefix("skill://"),
            mime_type=self._catalogue.mime(uri),
            text=body,
        )


class HideMirrorTools(Middleware):
    """Drop the resource-mirroring tools from ``tools/list`` for clients that
    read resources.

    Every tool this server has is a mirror: it exists only because some clients
    cannot read resources. Advertising both shapes to a client that has
    resources is noise -- two ways to ask one question, and the model has to
    pick one.

    Filtering the listing rather than registering conditionally is what keeps
    one server object correct for every client at once. The decision depends on
    who is asking, so it can only be made per request; registration happens once
    at boot, when there is no asker.

    They stay *callable* either way. Hiding a tool from a listing is a
    presentation choice; refusing to run one a client already knows about would
    be a different and worse contract.
    """

    def __init__(self, names: set[str]):
        self.names = set(names)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        if not self.names or not client_reads_resources():
            return tools
        return [tool for tool in tools if tool.name not in self.names]


def register(mcp: FastMCP, catalogue: Catalogue) -> None:
    """Publish the catalogue as ``skill://`` resources."""
    mcp.add_provider(CatalogueProvider(catalogue))
