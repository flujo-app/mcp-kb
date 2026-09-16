"""The MCP server itself: wiring, and nothing else.

Assembles a FastMCP instance from the catalogue a config file describes -- the
address space, the resources, the mirror tools, the routes -- and runs it on a
transport. Turning a config source into a directory is ``sources.py``'s job;
deciding what in it counts as a skill, a prompt or a pack-level file is
``harvest.py``'s; the catalogue itself lives in ``skills.py``, ``prompts.py``
and ``uris.py``. This module only walks the config in source order and
connects the pieces, so a new source scheme or a new kind of component never
means editing the server.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP

from . import harvest, prompts, resources, routes, tools
from .config import Config
from .prompts import FilePrompt, load_prompts
from .skills import PackResources, Skill, SkillIndex, load_skills
from .sources import SourceError, materialise_all
from .uris import Catalogue

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
    """An MCP server over the sources a config file names.

    The catalogue is served twice, because MCP clients are not all alike. As
    ``skill://`` **resources**, which is what it is; and as two **tools** that
    mirror those resources exactly, for the many clients that only implement
    tools -- n8n among them, to which a resource-only server looks empty.

    The mirror is hidden from clients that read resources, so each client sees
    one way to ask, not two. A client declares it cannot read resources with
    ``?resources=off`` on the MCP URL or an ``X-MCP-Resources: off`` header.
    """

    def __init__(self, config: Config, cache: Path):
        self.config = config
        self.cache = cache

        materialised = materialise_all(config, cache)
        skills: list[Skill] = []
        all_prompts: list[FilePrompt] = []
        self.resources = PackResources()
        self.status: dict[str, dict] = {}

        for source in config.sources:
            root = materialised[source.name]
            if isinstance(root, SourceError):
                self.status[source.name] = {"status": "failed", "error": str(root)}
                continue
            lib = config.library(source.library_name)
            tags = [*lib.tags, *source.tags]
            dirs = harvest.skill_dirs(root, source.include)
            loaded_skills = load_skills(
                dirs, pack=lib.name, source=source.name, root=root, tags=tags
            )
            skills += loaded_skills
            files = harvest.pack_files(root, source.include, dirs)
            self.resources.add(lib.name, root, files, dirs)
            loaded_prompts = load_prompts(
                harvest.prompt_files(root, source.include),
                pack=lib.name,
                source=source.name,
                tags=tags,
            )
            all_prompts += loaded_prompts
            self.status[source.name] = {
                "status": "ok",
                "library": lib.name,
                "skills": len(loaded_skills),
                "prompts": len(loaded_prompts),
                "files": len(files),
            }

        self.index = SkillIndex(skills)
        self.catalogue = Catalogue(self.index, self.resources)
        self.prompts = all_prompts
        self.mcp = FastMCP("mcp-school", instructions=INSTRUCTIONS)

        resources.register(self.mcp, self.catalogue)
        mirrors = tools.register(self.mcp, self.catalogue)
        self.mcp.add_middleware(resources.HideMirrorTools(mirrors))
        prompts.register(self.mcp, self.prompts, self.index)
        routes.register(self.mcp, self.index, self.prompts, self.status)

    def run(
        self, transport: str = "http", host: str = "0.0.0.0", port: int = 8000
    ) -> None:
        """Serve on ``transport``, blocking until the process is stopped."""
        if transport == "stdio":
            self.mcp.run(transport="stdio")
        else:
            self.mcp.run(transport="http", host=host, port=port)
