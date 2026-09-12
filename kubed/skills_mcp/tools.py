"""The tools, which are a mirror of the resources and nothing else.

Two tools, whatever the catalogue holds, and both are the resource interface
wearing a tool's clothes: ``list_resources`` returns the rows ``resources/list``
returns, ``read_resource`` takes the URI ``resources/read`` takes. An agent that
knows how to drive MCP resources already knows how to drive these, because the
vocabulary is the same one -- list addresses, read an address.

That is the whole design rule here. The previous surface invented a second
vocabulary (``read_skill(skill, file)``, ``read_pack_file(pack, file)``) for the
same act, which meant a model had to learn where a file lived before it could
ask for it -- and a reference inside a SKILL.md does not say which side of that
line it falls on.

Progressive disclosure survives the collapse, because it moved into the address
space rather than into the tool list::

    list_resources()                     -> ~12 indexes, one per pack and group
    read_resource("skill://grafana-lgtm") -> that group's skills, as URIs
    read_resource("skill://grafana/loki") -> the instructions to follow

Both tools are hidden from a client that reads resources; see
``resources.HideMirrorTools``.
"""

from __future__ import annotations

from fastmcp import FastMCP

from .request import requested_pack
from .uris import Catalogue

LIST_TOOL = "list_resources"
READ_TOOL = "read_resource"
MIRROR_TOOLS = {LIST_TOOL, READ_TOOL}


def register(mcp: FastMCP, catalogue: Catalogue) -> set[str]:
    """Register the mirror tools; return their names for the listing filter."""

    @mcp.tool
    def list_resources() -> list[dict]:
        """List the skill resources available, as `uri`/`name`/`description`.

        Start here. The listing is indexes, not skills: one row per pack and per
        group within a pack, plus one for any files a pack ships outside its
        skills. Read an index URI to get the skills inside it, then read a skill
        URI for its instructions.

        This returns exactly what an MCP `resources/list` would, so a `uri` from
        here can be read with `read_resource` or with your own resource reader.
        """
        return [entry.as_dict() for entry in catalogue.entries(requested_pack())]

    @mcp.tool
    def read_resource(uri: str) -> str:
        """Read one `skill://` URI: an index, a skill, or a file inside one.

        Args:
            uri: A URI from list_resources, or one built from this grammar:
                `skill://<pack>` or `skill://<group>` for an index,
                `skill://<pack>/<skill>` for that skill's instructions,
                `skill://<pack>/<skill>/_manifest` for what else it ships,
                `skill://<pack>/<skill>/<path>` for one of those files.

        Reads only what was asked for. A skill's instructions may cite
        `references/FOO.md`; citing it does not fetch it, so fetch it only if
        you are going to use it.
        """
        body = catalogue.read(uri, requested_pack())
        if body is None:
            return (
                f"No resource at '{uri}'. Call list_resources() for the indexes,"
                " then read one to see the URIs inside it."
            )
        return body

    return MIRROR_TOOLS
