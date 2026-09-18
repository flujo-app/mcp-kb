"""The shape every dialect parses into, and the bits all three share.

``Argument`` and ``Parsed`` live here rather than in the package's
``__init__`` so a dialect module can import them: ``__init__`` imports every
dialect at the top of the file, where imports belong, and a dialect importing
back from the half-initialised package would not find them yet. Both are
re-exported from ``kubed.mcp_kb.catalogue.prompts``, which stays the import
path everything outside this package uses.

The readers below -- ``text``, ``names``, ``objects`` -- are why no dialect
indexes or iterates a frontmatter value directly. Frontmatter is *fetched
content*: a field is whatever YAML made of whatever somebody wrote, so
``arguments: true`` is a file that will be published one day, and a dialect
that iterated it would raise ``TypeError`` out of ``load_prompts`` -- which
catches ``ValueError`` -- and take the cold start down over one file in
somebody else's repository. Every wrong type is refused as a ``ValueError``
naming the field instead, so that file is skipped with a reason an operator
can read in ``/health`` and every other prompt of the plugin still serves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Every dialect but this server's own publishes one argument for "whatever the
# person typed after the command", because that is the only thing those
# clients pass. It is named for Claude's `$ARGUMENTS`, which is where the
# convention comes from.
FREE_TEXT = "arguments"


@dataclass(frozen=True)
class Argument:
    """One declared placeholder: FastMCP-free twin of ``PromptArgument``."""

    name: str
    description: str | None = None
    required: bool = False


@dataclass(frozen=True)
class Parsed:
    """What one dialect read out of one file, before it becomes a ``FilePrompt``.

    ``dropped`` is the frontmatter keys this dialect carries that mean nothing
    over MCP -- ``model``, ``tools``, ``allowed-tools`` and the rest. They are
    reported once per file at DEBUG rather than vanishing silently, so an
    author who set one can find out it was ignored.
    """

    description: str | None
    title: str | None
    arguments: list[Argument]
    defaults: dict[str, str]
    dropped: tuple[str, ...] = ()


def kind(value: object) -> str:
    """What a field holds, in the words whoever wrote the file would use."""
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    return "text"


def text(value: object, field: str) -> str | None:
    """``value`` as one string, refusing a mapping or a list by naming ``field``.

    A scalar is taken as written -- ``version: 2`` and an unquoted date are
    what YAML made of them, and refusing those would be refusing the file over
    a quote mark. A mapping or a list is a different shape entirely, and
    ``str()`` over one publishes a Python repr as somebody's description.
    """
    if value is None:
        return None
    if isinstance(value, dict | list):
        raise ValueError(f"{field} must be text, not {kind(value)}")
    return str(value)


def names(value: object, field: str) -> list[str]:
    """``value`` as a list of names: one whitespace-separated string, or a list.

    Claude's ``arguments``, which declares names and nothing about them.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of names, not {kind(value)}")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"{field} must be a list of names, and one item is {kind(item)}"
            )
    return list(value)


def objects(value: object, field: str) -> list[dict]:
    """``value`` as a list of mappings: this server's own ``arguments``.

    Absent and empty both mean "none declared". Everything else is refused,
    including a bare string: iterating one would read an argument per
    character and declare four arguments for ``arguments: app``.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of objects, not {kind(value)}")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(
                f"{field} must be a list of objects, and one item is {kind(item)}"
            )
    return value


def description_of(meta: dict) -> str | None:
    """The description as one line: a folded YAML string arrives with newlines."""
    one_line = text(meta.get("description"), "description") or ""
    return " ".join(one_line.split()) or None


def dropped_keys(meta: dict, meaningful: Sequence[str]) -> tuple[str, ...]:
    """Every key this dialect cannot publish, in the order the file wrote them."""
    return tuple(str(key) for key in meta if key not in meaningful)


def hint_of(meta: dict) -> str | None:
    """``argument-hint`` as text, or None when the file has none.

    ``argument-hint: [file] [board]`` is a YAML *list* of two hints, not the
    string it looks like, so a hint is joined back into the line its author
    meant rather than published as a Python repr.
    """
    hint = meta.get("argument-hint")
    if isinstance(hint, list):
        parts = [text(item, "argument-hint") or "" for item in hint]
        return " ".join(parts)
    return text(hint, "argument-hint")


def free_text(hint: str | None) -> Argument:
    """The one argument a command with no declared names still takes.

    ``argument-hint`` is what its author wrote for the person filling it in --
    ``[file] [board]`` -- so it is the best description available; an empty one
    describes nothing, and then the argument says what it is.
    """
    return Argument(
        name=FREE_TEXT, description=hint or "Everything typed after the command."
    )
