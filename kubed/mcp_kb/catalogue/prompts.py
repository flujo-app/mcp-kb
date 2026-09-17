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

Placeholders are ``{{ name }}`` rather than ``str.format``'s ``{name}`` because
these bodies are full of LogQL, PromQL and JSON, which all use single braces.

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
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
import yaml

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class Argument:
    """One declared placeholder: FastMCP-free twin of ``PromptArgument``."""

    name: str
    description: str | None = None
    required: bool = False


@dataclass
class FilePrompt:
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
    name: str
    library: str
    source: str = ""
    template: str = ""
    description: str | None = None
    arguments: list[Argument] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    live: bool = False
    defaults: dict[str, str] = field(default_factory=dict)

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

    def render(self, arguments: dict[str, object] | None = None) -> str:
        """Fill in the placeholders, raising ``ValueError`` on a missing one.

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
            raise ValueError(f"Missing required arguments: {', '.join(missing)}")
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

    arguments: list[Argument] = []
    defaults: dict[str, str] = {}
    for raw in meta.get("arguments") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError(f"argument without a name: {raw!r}")
        name = str(raw["name"])
        required = bool(raw.get("required", False))
        if required and "default" in raw:
            raise ValueError(f"required argument '{name}' cannot have a default")
        arguments.append(
            Argument(name=name, description=raw.get("description"), required=required)
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
