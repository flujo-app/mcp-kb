"""The MCP server itself: wiring, and nothing else.

Assembles a FastMCP instance from the skill catalogue -- the address space, the
resources, the mirror tools, the routes -- and runs it on a transport. The
catalogue lives in ``skills.py``, the URI space in ``uris.py``, the tool bodies
in ``tools.py``, the prompts in ``prompts.py``, the endpoints in ``routes.py``;
this module only connects them, so a new tool never means editing the server.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP

from . import prompts, resources, routes, tools
from .prompts import load_prompts
from .skills import PackResources, SkillIndex, load_skills
from .uris import Catalogue

DEFAULT_SKILLS_DIR = Path("/skills")
DEFAULT_PROMPTS_DIR = Path("/prompts")

INSTRUCTIONS = """\
This server hosts Agent Skills: instruction packages that teach you how to \
perform a specific task. Everything it serves is a `skill://` URI, and reading \
one is the only operation there is.

Work down the address space, cheapest first. Listing gives you indexes -- one \
per pack, one per group within a pack. Reading an index URI \
(`skill://grafana-lgtm`) gives you the skills in it, as URIs. Reading a skill \
URI (`skill://grafana/loki`) gives you the instructions to follow.

A skill may ship supporting files. Append `/_manifest` to its URI to list them, \
then read one by its path under the same URI. Do not read files you have no use \
for -- a skill citing one is not a reason to fetch it.
"""


class School:
    """An MCP server over a directory of Agent Skills.

    The catalogue is served twice, because MCP clients are not all alike. As
    ``skill://`` **resources**, which is what it is; and as two **tools** that
    mirror those resources exactly, for the many clients that only implement
    tools -- n8n among them, to which a resource-only server looks empty.

    The mirror is hidden from clients that read resources, so each client sees
    one way to ask, not two. A client declares it cannot read resources with
    ``?resources=off`` on the MCP URL or an ``X-MCP-Resources: off`` header.
    """

    def __init__(
        self,
        skills_dir: Path = DEFAULT_SKILLS_DIR,
        packs: list[str] | None = None,
        prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    ):
        self.skills_dir = skills_dir
        self.packs = packs
        skills = load_skills(skills_dir, packs)
        self.index = SkillIndex(skills)
        self.resources = PackResources(skills_dir, skills)
        self.catalogue = Catalogue(self.index, self.resources)
        self.prompts = load_prompts(prompts_dir, packs)
        self.mcp = FastMCP("mcp-school", instructions=INSTRUCTIONS)

        resources.register(self.mcp, self.catalogue)
        mirrors = tools.register(self.mcp, self.catalogue)
        self.mcp.add_middleware(resources.HideMirrorTools(mirrors))
        prompts.register(self.mcp, self.prompts, self.index)
        routes.register(self.mcp, self.index, self.prompts)

    def run(
        self, transport: str = "http", host: str = "0.0.0.0", port: int = 8000
    ) -> None:
        """Serve on ``transport``, blocking until the process is stopped."""
        if transport == "stdio":
            self.mcp.run(transport="stdio")
        else:
            self.mcp.run(transport="http", host=host, port=port)
