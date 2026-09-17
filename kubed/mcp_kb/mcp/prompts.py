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

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError, ValidationError
from fastmcp.prompts import Prompt, PromptArgument
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers.base import Provider
from fastmcp.server.transforms import PromptsAsTools
from fastmcp.server.transforms.prompts_as_tools import _format_prompt_result
from fastmcp.tools.base import Tool
from fastmcp.utilities.versions import VersionSpec
from mcp_types import ToolAnnotations
from pydantic import ConfigDict

from ..catalogue.prompts import FilePrompt, MissingArguments
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

    A missing argument is the one failure translated here. FastMCP treats any
    other exception out of a render as its own fault: -32603 on the wire and a
    traceback at ERROR in the log. Its ``ValidationError`` is the caller's
    fault instead -- -32602 -- and logged at the level it carries.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    file: FilePrompt

    async def render(self, arguments: dict[str, object] | None = None) -> str:
        try:
            return self.file.render(arguments)
        except MissingArguments as exc:
            raise ValidationError(str(exc), log_level=logging.DEBUG) from exc


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
    resource mirror was corrected for.

    ``get_prompt`` is rebuilt rather than annotated, for its errors. FastMCP's
    lets an unknown name escape as a bare exception, which the tool runner logs
    as a traceback at ERROR and reports as "Error calling tool"; and a missing
    argument is logged as a warning about the tool's arguments, which it is
    not. Both are the caller's mistake, so both become an error result that says
    what to do, logged at DEBUG. What a successful call returns is FastMCP's.
    """

    def _make_list_prompts_tool(self) -> Tool:
        return _annotate(super()._make_list_prompts_tool(), "List prompts")

    def _make_get_prompt_tool(self) -> Tool:
        async def get_prompt(
            name: Annotated[str, "The name of the prompt to get"],
            arguments: Annotated[
                dict[str, Any] | None,
                "Optional arguments for the prompt",
            ] = None,
        ) -> str:
            """Get a prompt by name with optional arguments.

            Returns the rendered prompt as JSON with a messages array.
            Arguments should be provided as a dict mapping argument names
            to values.
            """
            ctx = get_context()
            try:
                result = await ctx.fastmcp.render_prompt(
                    name, arguments=arguments or {}
                )
            except NotFoundError:
                raise ToolError(
                    f"Unknown prompt: {name!r}. Call list_prompts() for the prompts"
                    " available and the arguments each one takes.",
                    log_level=logging.DEBUG,
                ) from None
            except ValidationError as exc:
                raise ToolError(str(exc), log_level=logging.DEBUG) from None
            return _format_prompt_result(result)

        return _annotate(Tool.from_function(fn=get_prompt), "Get a prompt")


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

