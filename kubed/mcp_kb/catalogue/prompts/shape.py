"""The shape every dialect parses into, and the bits all three share.

``Argument`` and ``Parsed`` live here rather than in the package's
``__init__`` so a dialect module can import them: ``__init__`` imports every
dialect at the top of the file, where imports belong, and a dialect importing
back from the half-initialised package would not find them yet. Both are
re-exported from ``kubed.mcp_kb.catalogue.prompts``, which stays the import
path everything outside this package uses.
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


def description_of(meta: dict) -> str | None:
    """The description as one line: a folded YAML string arrives with newlines."""
    return " ".join(str(meta.get("description", "")).split()) or None


def dropped_keys(meta: dict, meaningful: Sequence[str]) -> tuple[str, ...]:
    """Every key this dialect cannot publish, in the order the file wrote them."""
    return tuple(str(key) for key in meta if key not in meaningful)


def free_text(hint: str | None) -> Argument:
    """The one argument a command with no declared names still takes.

    ``argument-hint`` is what its author wrote for the person filling it in --
    ``[file] [board]`` -- so it is the best description available; without one,
    say what the argument is.
    """
    return Argument(
        name=FREE_TEXT, description=hint or "Everything typed after the command."
    )
