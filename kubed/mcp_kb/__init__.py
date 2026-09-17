"""Serve Agent Skills over MCP."""

from .catalogue.skills import PackResources, Skill, SkillIndex, load_skills
from .catalogue.snapshot import Snapshot
from .catalogue.uris import Catalogue, Entry
from .mcp.prompts import FilePrompt, load_prompts
from .server import KnowledgeBase

__all__ = [
    "Catalogue",
    "Entry",
    "FilePrompt",
    "KnowledgeBase",
    "PackResources",
    "Skill",
    "SkillIndex",
    "Snapshot",
    "load_prompts",
    "load_skills",
]
