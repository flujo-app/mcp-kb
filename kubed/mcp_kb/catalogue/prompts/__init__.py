"""Prompt files: parsed, with no FastMCP anywhere in the chain.

A prompt file is YAML frontmatter plus a body::

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

That is this server's own dialect, and it is one of three. Point the server at
a Claude Code command or a VS Code Copilot ``.prompt.md`` and the same MCP
prompt comes out: ``detect.py`` decides which dialect a file is written in and
``mcpkb.py``, ``claude.py`` and ``copilot.py`` each read one of them into the
same ``Parsed`` and render it back. None of them invents a schema -- the
target is the MCP prompt itself (§C1.34).

The exposed name is ``<library>_<file stem>``. Prompt names are one flat
namespace per server, and two libraries shipping a ``debug.md`` must not
collide the way two skills named ``testing`` otherwise would.

``FilePrompt`` here is a plain dataclass -- the catalogue's own parsed form,
with no pydantic and no FastMCP base class -- so ``catalogue/snapshot.py`` and
``catalogue/index.py`` can hold and serialise it without importing FastMCP at
all. ``kubed/mcp_kb/mcp/prompts.py`` is where a ``FilePrompt`` becomes
something FastMCP can serve.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
import yaml

from .. import placeholders
from . import claude, copilot, detect, mcpkb
from .mcpkb import PLACEHOLDER
from .shape import Argument, Parsed

log = logging.getLogger(__name__)

DIALECTS = {module.NAME: module for module in (mcpkb, claude, copilot)}

__all__ = [
    "DIALECTS",
    "PLACEHOLDER",
    "Argument",
    "FilePrompt",
    "MissingArguments",
    "Parsed",
    "load_prompt",
    "load_prompts",
    "stem",
]


@dataclass
class FilePrompt:
    """One prompt file, rendered by substituting its placeholders.

    ``path`` is where it was read from. It is carried on the prompt rather than
    only known to the harvester so a rebuild can record it in the index and
    re-parse exactly the files a plugin yielded last time.

    ``plugin``, ``category`` and ``tags`` are the plugin's: what a scope matches
    a prompt on, since a prompt has no library of its own beyond the one that
    named it.

    ``live`` says the file is revalidated against its server as it is rendered,
    so the *body* is re-read from disk instead of taken from ``template``. The
    arguments are not re-read: they are what the harvest recorded, so declaring
    a new one still needs a refresh.

    ``dialect`` is which module renders it, decided once at load: the body's
    placeholders mean different things in each, so it travels with the prompt
    rather than being sniffed again per render.
    """

    path: Path
    name: str
    library: str
    plugin: str = ""
    template: str = ""
    description: str | None = None
    title: str | None = None
    arguments: list[Argument] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    category: str | None = None
    live: bool = False
    defaults: dict[str, str] = field(default_factory=dict)
    dialect: str = mcpkb.NAME

    def body(self) -> str:
        """The template to render: off disk for a live prompt, memory otherwise.

        A file that has become unreadable or lost its frontmatter falls back to
        the body last harvested -- the same rule the rest of live mode follows,
        that a server in trouble degrades to the copy already known good.
        """
        if not self.live:
            return self.template
        try:
            return _split(self.path.read_text(encoding="utf-8"))[1]
        except (OSError, ValueError) as exc:
            log.warning("re-reading prompt %s: %s", self.path, exc)
            return self.template

    def render(
        self,
        arguments: dict[str, object] | None = None,
        *,
        plugin_root: str | None = None,
    ) -> str:
        """Fill in the placeholders, raising ``MissingArguments`` on a missing one.

        ``values`` is built from ``self.arguments`` rather than from what was
        passed, so it holds every declared argument, in the order the file
        declared them: Claude's `$1` is the first declared name, and that order
        is knowable nowhere else.

        Given ``plugin_root``, the placeholders naming this server's own
        material are resolved after the dialect's own (§C1.37); every other
        `${…}` is the client's and stays as written.

        Plain and synchronous: the FastMCP-facing wrapper is what turns this
        into the async ``render`` a ``Prompt`` subclass must provide, and what
        turns a raised ``ValueError`` into whatever error shape FastMCP wants.
        """
        # A field left blank arrives as "" from most prompt pickers, so it
        # counts as not given: a default applies, and a required argument
        # fails.
        given = {
            key: str(value)
            for key, value in (arguments or {}).items()
            if value not in (None, "")
        }
        missing = [
            arg.name for arg in self.arguments if arg.required and arg.name not in given
        ]
        if missing:
            raise MissingArguments(self.name, missing)
        values = {
            arg.name: given.get(arg.name, self.defaults.get(arg.name, ""))
            for arg in self.arguments
        }
        text = DIALECTS[self.dialect].substitute(self.body(), values)
        if plugin_root is not None:
            text = placeholders.substitute(text, plugin_root=plugin_root)
        return text


class MissingArguments(ValueError):
    """A render was asked for without a required argument: the caller's mistake."""

    def __init__(self, prompt: str, names: list[str]):
        self.prompt = prompt
        self.names = names
        super().__init__(
            f"Prompt {prompt!r} needs the argument"
            f"{'s' if len(names) > 1 else ''} {', '.join(names)}."
        )


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


def stem(path: Path) -> str:
    """The file's name without its extension, ``.prompt.md`` counting as one.

    ``investigate.prompt.md`` is Copilot's spelling of ``investigate``: the
    ``.prompt`` is the convention that marks the file, not part of what the
    prompt is called.
    """
    if path.name.endswith(detect.COPILOT_SUFFIX):
        return path.name[: -len(detect.COPILOT_SUFFIX)]
    return path.stem


def load_prompt(
    path: Path,
    library: str,
    *,
    plugin: str = "",
    tags: Sequence[str] = (),
    category: str | None = None,
    live: bool = False,
    dialect: str = detect.AUTO,
) -> FilePrompt:
    """Parse one prompt file, raising ValueError on anything malformed.

    ``library`` is the library this prompt joins; ``plugin``, ``tags`` and
    ``category`` are the plugin's, carried as given -- nothing is added to
    them. ``dialect`` is what the config says the file is written in, and
    ``"auto"`` -- the usual answer -- leaves it to ``detect``.
    """
    meta, body = _split(path.read_text(encoding="utf-8"))

    name = detect.dialect_for(path, meta, body, dialect)
    if name is None:
        raise ValueError(f"not a prompt: has `{detect.not_a_prompt(meta)}`")
    if name not in DIALECTS:
        raise ValueError(f"unknown dialect '{name}'")

    parsed = DIALECTS[name].parse(meta, body)
    if parsed.dropped:
        log.debug(
            "prompt %s: ignoring %s (no meaning over MCP)",
            path,
            ", ".join(parsed.dropped),
        )

    return FilePrompt(
        path=path,
        live=live,
        name=f"{library}_{stem(path)}",
        description=parsed.description,
        title=parsed.title,
        arguments=parsed.arguments,
        tags=set(tags),
        library=library,
        plugin=plugin,
        category=category,
        template=body,
        defaults=parsed.defaults,
        dialect=name,
    )


def load_prompts(
    files: Sequence[Path],
    *,
    library: str,
    plugin: str = "",
    tags: Sequence[str] = (),
    category: str | None = None,
    live: bool = False,
    dialect: str = detect.AUTO,
    skipped: list[tuple[Path, str]] | None = None,
) -> list[FilePrompt]:
    """Every prompt file ``harvest`` already found for one plugin, joined to a library.

    ``library`` is the library the prompts join. A broken file is skipped
    rather than raised: a bad prompt must not take the skills down with it.
    Given ``skipped``, each one is added to it as ``(path, reason)`` for the
    caller to report once, in ``/health`` and the log; without it, it is logged
    here.
    """
    prompts: list[FilePrompt] = []
    for path in sorted(files):
        try:
            prompts.append(
                load_prompt(
                    path,
                    library,
                    plugin=plugin,
                    tags=tags,
                    category=category,
                    live=live,
                    dialect=dialect,
                )
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            if skipped is None:
                log.warning("skipping prompt %s: %s", path, exc)
            else:
                skipped.append((path, str(exc)))
    return prompts
