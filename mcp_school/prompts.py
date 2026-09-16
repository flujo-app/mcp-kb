"""Prompts: templates a client offers its *user*, served from markdown files.

A skill is read by the model when it decides to. A prompt is picked by a person
-- Claude Code lists them as slash commands -- who fills in a few arguments
before the model sees anything. Different primitive, so a different module, but
scoped by the same pack (the library) and ``X-Skill-Pack`` rules as skills:
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

The exposed name is ``<pack>_<file stem>``. Prompt names are one flat namespace
per server, and two packs shipping a ``debug.md`` must not collide the way two
skills named ``testing`` used to.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

import frontmatter
import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import PromptError
from fastmcp.prompts import Prompt, PromptArgument
from fastmcp.server.providers.base import Provider
from pydantic import Field

from .request import requested_pack
from .skills import SkillIndex

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class FilePrompt(Prompt):
    """One prompt file, rendered by substituting its placeholders."""

    pack: str
    source: str = ""
    template: str
    defaults: dict[str, str] = Field(default_factory=dict)

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
        return PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), self.template)


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
    path: Path, pack: str, *, source: str = "", tags: Sequence[str] = ()
) -> FilePrompt:
    """Parse one prompt file, raising ValueError on anything malformed.

    ``pack`` is the library this prompt joins; ``source`` and ``tags`` (the
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
        name=f"{pack}_{path.stem}",
        description=" ".join(str(meta.get("description", "")).split()) or None,
        arguments=arguments,
        tags={t for t in (pack, source, "prompt", *tags) if t},
        pack=pack,
        source=source,
        template=body,
        defaults=defaults,
    )


def load_prompts(
    files: Sequence[Path], *, pack: str, source: str, tags: Sequence[str] = ()
) -> list[FilePrompt]:
    """Every prompt file ``harvest`` already found for one source, joined to ``pack``.

    ``pack`` is the library the prompts join. A broken file is logged and
    skipped rather than raised: a bad prompt must not take the skills down with
    it. ``tests/test_prompts.py`` loads every shipped prompt strictly, so this
    only ever fires for a file mounted in at runtime.
    """
    prompts: list[FilePrompt] = []
    for path in sorted(files):
        try:
            prompts.append(load_prompt(path, pack, source=source, tags=tags))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning("skipping prompt %s: %s", path, exc)
    return prompts


class PromptProvider(Provider):
    """The prompts, scoped to whoever is asking.

    Only the listing is overridden. FastMCP's default ``_get_prompt`` looks a
    name up in that same listing, so a prompt outside this client's scope is
    unknown to ``prompts/get`` too, not merely unlisted.
    """

    def __init__(self, prompts: Sequence[FilePrompt], index: SkillIndex):
        super().__init__()
        self._prompts = list(prompts)
        self._index = index

    def visible(self, pinned: str = "") -> list[FilePrompt]:
        if not pinned:
            return list(self._prompts)
        # A prompt belongs to a pack, not a group, so a group pin sees its pack's
        # prompts -- the same rule pack-level files follow.
        packs = {s.pack for s in self._index.visible(pinned)}
        return [p for p in self._prompts if p.pack in packs]

    async def _list_prompts(self) -> Sequence[Prompt]:
        return self.visible(requested_pack())


def register(mcp: FastMCP, prompts: Sequence[FilePrompt], index: SkillIndex) -> None:
    """Publish the prompts."""
    mcp.add_provider(PromptProvider(prompts, index))
