"""Prompts: templates a client offers its *user*, served from markdown files.

A skill is read by the model when it decides to. A prompt is picked by a person
-- Claude Code lists them as slash commands -- who fills in a few arguments
before the model sees anything. Different primitive, so a different module, but
scoped by the same library and ``X-Skill-Library`` rules as skills:
``harvest.py`` finds the files per source, and this module turns them into
prompts joined to that source's library.

A file is YAML frontmatter plus a body::

    ---
    description: One line for the prompt picker.
    arguments:
    - name: app
      description: Shown to whoever fills it in.
      required: true
    - name: since
      default: 1h
    ---
    Investigate {{ app }} over the last {{ since }}.

Placeholders are ``{{ name }}`` rather than ``str.format``'s ``{name}`` because
these bodies are full of LogQL, PromQL and JSON, which all use single braces.

The exposed name is ``<library>_<file stem>``. Prompt names are one flat
namespace per server, and two libraries shipping a ``debug.md`` must not
collide the way two skills named ``testing`` otherwise would.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import PromptError
from fastmcp.prompts import Prompt, PromptArgument
from fastmcp.server.providers.base import Provider
from fastmcp.server.transforms import PromptsAsTools
from fastmcp.tools.base import Tool
from fastmcp.utilities.versions import VersionSpec
from mcp_types import ToolAnnotations
from pydantic import Field

from ..catalogue.skills import SkillIndex
from .request import requested_scope
from .scope import EVERYTHING, Scope
from .tools import READ_ONLY

if TYPE_CHECKING:
    from ..catalogue.snapshot import Snapshot

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class FilePrompt(Prompt):
    """One prompt file, rendered by substituting its placeholders.

    ``path`` is where it was read from. It is carried on the prompt rather than
    only known to the harvester so a rebuild can record it in the index and
    re-parse exactly the files a source yielded last time.

    ``live`` says the file is revalidated against its server as it is rendered,
    so the *body* is re-read from disk instead of taken from ``template``. The
    arguments are not re-read: they are what the harvest recorded, so declaring
    a new one still needs a refresh.
    """

    path: Path
    library: str
    source: str = ""
    template: str
    live: bool = False
    defaults: dict[str, str] = Field(default_factory=dict)

    def body(self) -> str:
        """The template to render: off disk for a live prompt, memory otherwise.

        A file that has become unreadable or lost its frontmatter falls back to
        the body last harvested -- the same rule the rest of live mode follows,
        that a source in trouble degrades to the copy already known good.
        """
        if not self.live:
            return self.template
        try:
            return _split(self.path.read_text(encoding="utf-8"))[1]
        except (OSError, ValueError) as exc:
            log.warning("re-reading prompt %s: %s", self.path, exc)
            return self.template

    async def render(self, arguments: dict[str, object] | None = None) -> str:
        # A field left blank arrives as "" from most prompt pickers, so it counts
        # as not given: a default applies, and a required argument fails.
        given = {
            key: str(value)
            for key, value in (arguments or {}).items()
            if value not in (None, "")
        }
        missing = [
            arg.name
            for arg in self.arguments or []
            if arg.required and arg.name not in given
        ]
        if missing:
            raise PromptError(f"Missing required arguments: {', '.join(missing)}")
        values = {**self.defaults, **given}
        return PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), self.body())


def _split(text: str) -> tuple[dict, str]:
    """Frontmatter and body, raising ValueError when there is no frontmatter block.

    ``frontmatter.loads`` returns empty metadata both for "no block" and for an
    "empty block" (``---\\n---\\nbody``) -- the latter is valid, so absence is
    told apart by comparing the stripped content back against the stripped
    whole text: only "no block at all" (including an unterminated one, which
    the library also parses as empty metadata over the whole text) leaves them
    equal. ``.content`` is ``rstrip``-ped by the library, which would silently
    drop a template's trailing newline; the body is re-sliced from the original
    text by length instead of by ``str.index``, which would find the first
    occurrence of the content anywhere -- including inside the frontmatter
    block itself, when the body text happens to recur there -- so a LogQL line
    like ``|= "error"\\n`` renders exactly as written.
    """
    post = frontmatter.loads(text)
    if post.metadata == {} and post.content.strip() == text.strip():
        raise ValueError("no YAML frontmatter")
    meta = post.metadata if isinstance(post.metadata, dict) else {}
    if not post.content:
        return meta, ""
    start = len(text.rstrip()) - len(post.content)
    return meta, text[start:]


def load_prompt(
    path: Path,
    library: str,
    *,
    source: str = "",
    tags: Sequence[str] = (),
    live: bool = False,
) -> FilePrompt:
    """Parse one prompt file, raising ValueError on anything malformed.

    ``library`` is the library this prompt joins; ``source`` and ``tags`` (the
    library's tags plus the source's, concatenated by the caller) become part
    of every prompt's own tags. An undeclared placeholder is an error rather
    than an empty substitution: it is almost always a typo, and rendered blank
    it produces a prompt that reads fine and asks the model for the wrong thing.
    """
    meta, body = _split(path.read_text(encoding="utf-8"))

    arguments: list[PromptArgument] = []
    defaults: dict[str, str] = {}
    for raw in meta.get("arguments") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError(f"argument without a name: {raw!r}")
        name = str(raw["name"])
        required = bool(raw.get("required", False))
        if required and "default" in raw:
            raise ValueError(f"required argument '{name}' cannot have a default")
        arguments.append(
            PromptArgument(
                name=name, description=raw.get("description"), required=required
            )
        )
        if "default" in raw:
            defaults[name] = str(raw["default"])

    undeclared = sorted(set(PLACEHOLDER.findall(body)) - {a.name for a in arguments})
    if undeclared:
        raise ValueError(f"placeholders with no argument: {', '.join(undeclared)}")

    return FilePrompt(
        path=path,
        live=live,
        name=f"{library}_{path.stem}",
        description=" ".join(str(meta.get("description", "")).split()) or None,
        arguments=arguments,
        tags={t for t in (library, source, "prompt", *tags) if t},
        library=library,
        source=source,
        template=body,
        defaults=defaults,
    )


def load_prompts(
    files: Sequence[Path],
    *,
    library: str,
    source: str,
    tags: Sequence[str] = (),
    live: bool = False,
) -> list[FilePrompt]:
    """Every prompt file ``harvest`` already found for one source, joined to a library.

    ``library`` is the library the prompts join. A broken file is logged and
    skipped rather than raised: a bad prompt must not take the skills down with
    it. ``tests/test_prompts.py`` loads every shipped prompt strictly, so this
    only ever fires for a file mounted in at runtime.
    """
    prompts: list[FilePrompt] = []
    for path in sorted(files):
        try:
            prompts.append(
                load_prompt(path, library, source=source, tags=tags, live=live)
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning("skipping prompt %s: %s", path, exc)
    return prompts


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
        libraries = _libraries_of(scope.library, snapshot.index)
        tags = Scope(tags=scope.tags)
        return [
            p
            for p in prompts
            if (not libraries or p.library in libraries)
            and tags.admits(p.library, p.tags)
        ]

    async def _list_prompts(self) -> Sequence[Prompt]:
        return self.visible(requested_scope())

    async def _get_prompt(
        self, name: str, version: VersionSpec | None = None
    ) -> Prompt | None:
        """The prompt, revalidated first if it came from a live source.

        ``render`` is where the body is read, and it is not this provider's to
        call -- FastMCP renders what it is handed. So the file is brought level
        with its server here, on the way out.
        """
        prompt = await super()._get_prompt(name, version)
        if isinstance(prompt, FilePrompt):
            self._snapshot().revalidate(prompt.path)
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


def _libraries_of(selector: str, index: SkillIndex) -> frozenset[str]:
    """The libraries a scope's ``library`` selects, for matching prompts.

    A prompt belongs to a library, not a group, so a group name resolves to the
    libraries holding that group -- every one of them, since group names are
    not unique across libraries and resources already admit them all. The
    selector also always counts as a library name in its own right, or a library
    of prompts and no skills would vanish whenever some other library happened
    to have a group by the same name.
    """
    if not selector:
        return frozenset()
    return frozenset(s.library for s in index.visible(Scope(selector))) | {selector}
