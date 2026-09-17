"""An MCP knowledge base: skills, prompts and agent material in one catalogue.

The names below are re-exported *lazily*, because importing one layer must not
import them all: ``kubed.mcp_kb.config`` is the config models and nothing else
-- the schema generator, the plugin resolution and their tests read it -- while
``KnowledgeBase`` pulls in the whole server and FastMCP behind it.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "Catalogue": ".catalogue.uris",
    "Entry": ".catalogue.uris",
    "FilePrompt": ".catalogue.prompts",
    "KnowledgeBase": ".server",
    "LibraryFiles": ".catalogue.skills",
    "Skill": ".catalogue.skills",
    "SkillIndex": ".catalogue.skills",
    "Snapshot": ".catalogue.snapshot",
    "load_prompts": ".catalogue.prompts",
    "load_skills": ".catalogue.skills",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> object:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(_EXPORTS[name], __name__), name)


def __dir__() -> list[str]:
    return list(__all__)
