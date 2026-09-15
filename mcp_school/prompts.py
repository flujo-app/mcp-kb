"""Prompts: templates a client offers its *user*, served from markdown files.

A skill is read by the model when it decides to. A prompt is picked by a person
-- Claude Code lists them as slash commands -- who fills in a few arguments
before the model sees anything. Different primitive, so a different module, but
organised the way skills are: one folder per pack, ``prompts/<pack>/<name>.md``,
scoped by the same ``SKILL_PACKS`` and ``X-Skill-Pack`` rules.

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


def _split(text: str) -> tuple[dict, str] | None:
    """Frontmatter and body, or None when there is no frontmatter block."""
    if not text.startswith("---"):
        return None
    _, _, rest = text.partition("---")
    block, sep, body = rest.partition("\n---")
    if not sep:
        return None
    meta = yaml.safe_load(block) or {}
    return (meta if isinstance(meta, dict) else {}), body.lstrip("\n")


def load_prompt(path: Path, pack: str) -> FilePrompt:
    """Parse one prompt file, raising ValueError on anything malformed.

    An undeclared placeholder is an error rather than an empty substitution: it
    is almost always a typo, and rendered blank it produces a prompt that reads
    fine and asks the model for the wrong thing.
    """
    split = _split(path.read_text(encoding="utf-8"))
    if split is None:
        raise ValueError("no YAML frontmatter")
    meta, body = split

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
        tags={pack},
        pack=pack,
        template=body,
        defaults=defaults,
    )


def load_prompts(base: Path, packs: list[str] | None = None) -> list[FilePrompt]:
    """Every prompt under ``base``, as ``<pack>/<name>.md``.

    A broken file is logged and skipped rather than raised: a bad prompt must not
    take the skills down with it. ``tests/test_prompts.py`` loads every shipped
    prompt strictly, so this only ever fires for a file mounted in at runtime.
    """
    if not base.is_dir():
        return []
    prompts: list[FilePrompt] = []
    for path in sorted(base.glob("*/*.md")):
        pack = path.parent.name
        if packs and pack not in packs:
            continue
        try:
            prompts.append(load_prompt(path, pack))
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
