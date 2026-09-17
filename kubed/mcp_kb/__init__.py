"""An MCP knowledge base: skills, prompts and agent material in one catalogue."""

from .catalogue.prompts import FilePrompt, load_prompts
from .catalogue.skills import LibraryFiles, Skill, SkillIndex, load_skills
from .catalogue.snapshot import Snapshot
from .catalogue.uris import Catalogue, Entry
from .server import KnowledgeBase

__all__ = [
    "Catalogue",
    "Entry",
    "FilePrompt",
    "KnowledgeBase",
    "LibraryFiles",
    "Skill",
    "SkillIndex",
    "Snapshot",
    "load_prompts",
    "load_skills",
]
