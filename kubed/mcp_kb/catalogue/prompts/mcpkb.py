"""This server's own prompt files: ``{{ name }}`` and arguments as objects.

The richest of the dialects, and the only one that can mark an argument
*required* or give it a default -- which is why an undeclared placeholder is
an error here and nowhere else: this is the one format where the author had a
way to declare it, so a `{{ nmae }}` is a typo, and rendered blank it produces
a prompt that reads fine and asks the model for the wrong thing.
"""

from __future__ import annotations

import re

from .shape import Argument, Parsed, description_of, dropped_keys

NAME = "mcp-kb"

# `{{ name }}` rather than str.format's `{name}` because these bodies are full
# of LogQL, PromQL and JSON, which all use single braces.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

MEANINGFUL = ("description", "title", "arguments")


def parse(meta: dict, body: str) -> Parsed:
    """The arguments as declared, refusing a file that contradicts itself."""
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

    title = meta.get("title")
    return Parsed(
        description=description_of(meta),
        title=None if title is None else str(title),
        arguments=arguments,
        defaults=defaults,
        dropped=dropped_keys(meta, MEANINGFUL),
    )


def substitute(body: str, values: dict[str, str]) -> str:
    """Every placeholder is declared, so every one of them has a value here."""
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), body)
