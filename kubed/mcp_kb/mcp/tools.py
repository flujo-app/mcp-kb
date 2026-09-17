"""The tools, which are a mirror of the resources and nothing else.

Two tools, whatever the catalogue holds, and both are the resource interface
wearing a tool's clothes: ``list_resources`` returns the rows ``resources/list``
returns, ``read_resource`` takes the URI ``resources/read`` takes. An agent that
knows how to drive MCP resources already knows how to drive these, because the
vocabulary is the same one -- list addresses, read an address.

That is the whole design rule here. A second vocabulary for the same act --
``read_skill(skill, file)`` beside ``read_library_file(library, file)`` -- makes
a model learn where a file lives before it can ask for it, and a reference
inside a SKILL.md does not say which side of that line it falls on.

Progressive disclosure survives the collapse, because it moved into the address
space rather than into the tool list::

    list_resources()
        -> ~12 indexes, one per library and folder
    read_resource("skill://grafana/grafana-lgtm/_index.md")
        -> that folder's skills, as URIs
    read_resource("skill://grafana/grafana-lgtm/loki/SKILL.md")
        -> the instructions to follow

Both tools are hidden from a client that reads resources; see
``resources.HideMirrorTools``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..catalogue.uris import Catalogue
from .request import full_listing, requested_scope

LIST_TOOL = "list_resources"
READ_TOOL = "read_resource"
MIRROR_TOOLS = {LIST_TOOL, READ_TOOL}

# Both only read the catalogue this pod already harvested: nothing changes, a
# repeat call gives the same answer, and no source is reached to serve one. Left
# off, MCP's defaults advertise a tool as destructive, and a client may confirm
# every read.
READ_ONLY = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": False,
}


def register(mcp: FastMCP, catalogue: Callable[[], Catalogue]) -> set[str]:
    """Register the mirror tools; return their names for the listing filter.

    ``catalogue`` is a getter for the same reason it is one in ``resources.py``:
    a closure over the object would pin these tools to the generation they were
    registered in.
    """

    @mcp.tool(annotations={"title": "List skill resources", **READ_ONLY})
    def list_resources() -> list[dict[str, str]]:
        """List the skill resources available, as `uri`/`name`/`description`.

        Start here. The listing is indexes, not skills: a
        `skill://<library>/_index.md` per library, a
        `skill://<library>/<folder>/_index.md` per top-level folder, and a
        `skill://<library>/_files.md` for a library that ships files outside
        its skills. Read an index for the URIs inside it, then read a skill's
        `SKILL.md` URI for its instructions. What is listed is what this
        connection's scope admits: `?library=`, `?categories=` and `?tags=` on
        the MCP URL narrow it, and that ceiling is not yours to widen.

        This returns exactly what an MCP `resources/list` would, so a `uri` from
        here can be read with `read_resource` or with your own resource reader.
        """
        entries = catalogue().entries(requested_scope(), full=full_listing())
        return [entry.as_dict() for entry in entries]

    @mcp.tool(annotations={"title": "Read a skill resource", **READ_ONLY})
    def read_resource(uri: str) -> str:
        """Read one `skill://` URI: an index, a skill's file, or a library's file.

        Args:
            uri: A URI from list_resources or an index, or one built from this
                grammar, where `<folder>` may be empty or several segments:
                `skill://<library>/_index.md` or
                `skill://<library>/<folder>/_index.md` for an index of skills,
                `skill://<library>/<folder>/<skill>/SKILL.md` for a skill's
                instructions,
                `skill://<library>/<folder>/<skill>/_manifest` for what else it
                ships, and `skill://<library>/<folder>/<skill>/<path>` for one
                of those files,
                `skill://<library>/_files.md` for the files a library ships
                outside its skills, and `skill://<library>/<path>` for one of
                those.

        Only files are read. A library, a folder or a skill without a file
        named after it is a directory, and reading one says which file to read
        instead.

        Reads only what was asked for. A skill's instructions may cite
        `references/FOO.md`; citing it does not fetch it, so fetch it only if
        you are going to use it.
        """
        current, scope = catalogue(), requested_scope()
        body = current.read(uri, scope)
        if body is not None:
            return body
        hint = current.hint(uri, scope) or (
            "Call list_resources() for the indexes, then read one to see the URIs"
            " inside it."
        )
        # An error result, so a caller that branches on isError can tell a miss
        # from a file; the hint rides in its message. The caller's mistake, not
        # the server's, so it logs at DEBUG and never as a traceback.
        raise ToolError(f"No resource at '{uri}'. {hint}", log_level=logging.DEBUG)

    return MIRROR_TOOLS
