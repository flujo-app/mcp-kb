"""Prompts as FastMCP serves them: templates a client offers its *user*.

A skill is read by the model when it decides to. A prompt is picked by a person
-- Claude Code lists them as slash commands -- who fills in a few arguments
before the model sees anything. Different primitive, so a different module,
scoped by the same library and ``X-Skill-Library`` rules as skills.

The parsing -- frontmatter, placeholders, the loader -- lives in
``catalogue/prompts.py`` and has no FastMCP in it at all: ``catalogue/`` is the
layer ``AGENTS.md`` promises stays free of it. This module is the seam:
``MCPFilePrompt`` wraps a catalogue ``FilePrompt`` in FastMCP's own ``Prompt``
base class, and ``PromptProvider`` is what ``resources.py``'s sibling for
skills is for prompts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.prompts import Prompt, PromptArgument
from fastmcp.server.providers.base import Provider
from fastmcp.server.transforms import PromptsAsTools
from fastmcp.tools.base import Tool
from fastmcp.utilities.versions import VersionSpec
from mcp_types import ToolAnnotations
from pydantic import ConfigDict

from ..catalogue.prompts import FilePrompt
from .request import requested_scope
from .scope import EVERYTHING, Scope
from .tools import READ_ONLY

if TYPE_CHECKING:
    from ..catalogue.snapshot import Snapshot


class MCPFilePrompt(Prompt):
    """FastMCP's ``Prompt``, backed by a catalogue ``FilePrompt``.

    Everything a client-facing prompt needs -- the substitution, the missing-
    argument check, the live re-read -- is ``file``'s to do; this class exists
    only to satisfy FastMCP's ``Prompt`` contract and hand the result back.
    Raising ``ValueError`` here is deliberate rather than FastMCP's own
    ``PromptError``: the server already wraps any exception a render raises
    into a ``PromptError`` naming the prompt, so ``file.render`` stays free of
    a FastMCP import for the one thing it can fail at.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    file: FilePrompt

    async def render(self, arguments: dict[str, object] | None = None) -> str:
        return self.file.render(arguments)


def _adapt(file: FilePrompt) -> MCPFilePrompt:
    return MCPFilePrompt(
        name=file.name,
        description=file.description,
        tags=set(file.tags),
        arguments=[
            PromptArgument(name=a.name, description=a.description, required=a.required)
            for a in file.arguments
        ],
        file=file,
    )


class PromptProvider(Provider):
    """The prompts, scoped to whoever is asking.

    Only the listing is overridden. FastMCP's default ``_get_prompt`` looks a
    name up in that same listing, so a prompt outside this client's scope is
    unknown to ``prompts/get`` too, not merely unlisted.

    Takes one getter for the whole ``Snapshot`` rather than one per field. A
    refresh swaps the server's snapshot with a single assignment; reading the
    prompts and the index through two separate getters could straddle that
    swap and mix generations (prompts from N, index from N+1) -- the one thing
    every other reader in this codebase (``resources.py``, ``routes.py``)
    already avoids by taking one reference and working off it.
    """

    def __init__(self, snapshot: Callable[[], Snapshot]):
        super().__init__()
        self._snapshot = snapshot

    def visible(self, scope: Scope = EVERYTHING) -> list[FilePrompt]:
        snapshot = self._snapshot()
        prompts = list(snapshot.prompts)
        if not scope:
            return prompts
        sources = snapshot.index.sources(scope)
        return [
            p
            for p in prompts
            if (not scope.library or p.library == scope.library_name)
            and (sources is None or p.source in sources)
            and scope.admits_tags(p.tags)
        ]

    async def _list_prompts(self) -> Sequence[Prompt]:
        return [_adapt(p) for p in self.visible(requested_scope())]

    async def _get_prompt(
        self, name: str, version: VersionSpec | None = None
    ) -> Prompt | None:
        """The prompt, revalidated first if it came from a live source.

        ``render`` is where the body is read, and it is not this provider's to
        call -- FastMCP renders what it is handed. So the file is brought level
        with its server here, on the way out.
        """
        prompt = await super()._get_prompt(name, version)
        if isinstance(prompt, MCPFilePrompt):
            self._snapshot().revalidate(prompt.file.path)
        return prompt


class ReadOnlyPromptsAsTools(PromptsAsTools):
    """FastMCP's prompt tools, annotated as the read-only calls they are.

    Its generated tools carry no annotations, and unannotated MCP defaults
    advertise a tool as destructive and non-idempotent -- the same mistake the
    resource mirror was corrected for. Only the annotations change; what the
    tools do and return is FastMCP's.
    """

    def _make_list_prompts_tool(self) -> Tool:
        return _annotate(super()._make_list_prompts_tool(), "List prompts")

    def _make_get_prompt_tool(self) -> Tool:
        return _annotate(super()._make_get_prompt_tool(), "Get a prompt")


def _annotate(tool: Tool, title: str) -> Tool:
    return tool.model_copy(
        update={"annotations": ToolAnnotations(title=title, **READ_ONLY)}
    )


# FastMCP's PromptsAsTools names. Its tools route through the server's own
# prompts/list and prompts/get, so this provider's scoping applies to them too.
PROMPT_TOOLS = frozenset({"list_prompts", "get_prompt"})


def register(mcp: FastMCP, snapshot: Callable[[], Snapshot]) -> set[str]:
    """Publish the prompts, and their tool mirror; return the mirror's names.

    The mirror is FastMCP's own ``PromptsAsTools`` rather than one written here:
    it keeps what a prompt actually is -- role-tagged messages, several of them
    if the prompt has several -- which a resource or a hand-rolled tool would
    flatten to text.
    """
    mcp.add_provider(PromptProvider(snapshot))
    mcp.add_transform(ReadOnlyPromptsAsTools(mcp))
    return set(PROMPT_TOOLS)

