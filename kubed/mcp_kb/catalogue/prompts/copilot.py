"""VS Code Copilot `.prompt.md` files: `${input:name:placeholder}`.

Implemented alongside Claude's to prove the dialect seam is real rather than a
single-format loader with extra steps (§C1.35 point 6). Copilot declares
nothing in frontmatter: the arguments *are* the body's inputs, and the
placeholder text VS Code shows in its input box is the only description an
author writes, so it becomes the argument's.

``${selection}``, ``${file}``, ``${workspaceFolder}``, ``#file:`` and
``#tool:`` name things only the editor has, and are served as written.
"""

from __future__ import annotations

import re

from .shape import FREE_TEXT, Argument, Parsed, description_of, dropped_keys, free_text

NAME = "copilot"

MEANINGFUL = ("description", "argument-hint")

# `${input:name}` or `${input:name:the text VS Code shows}`. The text runs to
# the closing brace, so it may contain colons but not braces -- which is VS
# Code's own rule for it.
INPUT = re.compile(r"\$\{input:([A-Za-z0-9_][A-Za-z0-9_-]*)(?::([^}]*))?\}")


def parse(meta: dict, body: str) -> Parsed:
    """One argument per distinct input, in first-appearance order."""
    found: dict[str, str | None] = {}
    for match in INPUT.finditer(body):
        name, placeholder = match.group(1), match.group(2) or None
        if name not in found:
            found[name] = placeholder
        elif found[name] is None:
            # A later spelling of the same input may be the one that describes
            # it: `${input:app}` first, `${input:app:The workload.}` after.
            found[name] = placeholder

    arguments = [Argument(name=n, description=d) for n, d in found.items()]

    hint = meta.get("argument-hint")
    if not arguments and hint is not None:
        arguments.append(free_text(str(hint)))

    return Parsed(
        description=description_of(meta),
        title=None,
        arguments=arguments,
        defaults={},
        dropped=dropped_keys(meta, MEANINGFUL),
    )


def substitute(body: str, values: dict[str, str]) -> str:
    """Both spellings of a declared input take its value; anything else stands."""
    filled = INPUT.sub(lambda m: values.get(m.group(1), m.group(0)), body)
    free = values.get(FREE_TEXT, "")
    if free and not INPUT.search(body):
        # A body with no inputs places nothing, so the free text of an
        # `argument-hint` goes on the end, unlabelled: VS Code has no
        # `$ARGUMENTS` to name it after.
        return f"{filled}\n\n{free}"
    return filled
