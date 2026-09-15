"""Serve Agent Skills over MCP."""

from .prompts import FilePrompt, load_prompts
from .server import School
from .skills import PackResources, Skill, SkillIndex, load_skills
from .uris import Catalogue, Entry

__all__ = [
    "Catalogue",
    "Entry",
    "FilePrompt",
    "PackResources",
    "School",
    "Skill",
    "SkillIndex",
    "load_prompts",
    "load_skills",
]
