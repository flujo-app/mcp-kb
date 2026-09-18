"""Claude Code commands: declared names, `$ARGUMENTS`, and `$0`-`$9`.

The dialect the marketplaces ship, so it is the one that has to be exactly
right (§C1.35 point 6). A command declares names and nothing about them, so
every argument here is optional with no description: those clients tolerate an
unfilled placeholder, and inventing a description from the name would publish
a sentence the author never wrote.

``${CLAUDE_*}`` placeholders, `@path` references and `!` shell lines are not
this module's business -- the first are resolved (or left) by
``catalogue/placeholders.py`` and the rest are text, forever (§C1.37).
"""

from __future__ import annotations

import re

from .shape import (
    FREE_TEXT,
    Argument,
    Parsed,
    description_of,
    dropped_keys,
    free_text,
    hint_of,
)

# `substitute` keeps a local `names`, which is the declaration order of one
# file; the reader is the module-wide one. Renamed so neither shadows the other.
from .shape import names as declared_names

NAME = "claude"

MEANINGFUL = ("description", "arguments", "argument-hint")

# A digit followed by another digit or by `.<digit>` is not a positional:
# `$5.00` is a price and `$12` is not `$1`. `$1.` ending a sentence still is.
_NTH = r"\$([0-9])(?!\d)(?!\.\d)"

# The forms that stand for what was typed after the command. A body carrying
# one of them places the free text itself, so nothing is appended.
POSITIONAL = re.compile(rf"\$ARGUMENTS\[\d+\]|\$ARGUMENTS\b|{_NTH}")

TOKEN = re.compile(
    r"\$ARGUMENTS\[(\d+)\]"  # 1: the argument at that index
    r"|\$ARGUMENTS\b"  # everything typed after the command
    rf"|{_NTH}"  # 2: the same index, in its shorthand
    r"|\$([A-Za-z_][A-Za-z0-9_]*)"  # 3: a name
)


def parse(meta: dict, body: str) -> Parsed:
    """The declared names in order, plus the free text when the body wants it."""
    declared = declared_names(meta.get("arguments"), "arguments")
    arguments = [Argument(name=name) for name in declared]

    # Read whether or not it is published: a wrong-typed field is the file's
    # defect either way, and one refused only when the body happens to need it
    # is a file that serves today and is skipped the day a `$1` is added.
    hint = hint_of(meta)
    if FREE_TEXT not in declared and (not declared or POSITIONAL.search(body)):
        arguments.append(free_text(hint))

    return Parsed(
        description=description_of(meta),
        title=None,
        arguments=arguments,
        defaults={},
        dropped=dropped_keys(meta, MEANINGFUL),
    )


def substitute(body: str, values: dict[str, str]) -> str:
    """Fill the names, the positionals and the free text; leave the rest alone.

    ``values`` is every declared argument in declaration order (the contract
    ``FilePrompt.render`` keeps), which is what makes `$0` resolvable: it is
    the first declared name, and that order is only knowable from the file.
    """
    # The free text is the argument `parse` appends, which is always the last
    # one -- and `parse` appends none to a file that declares that name
    # itself. So a declared `arguments` ahead of another name is a name like
    # any other, and the indexes count over it rather than skipping it.
    names = list(values)
    if names and names[-1] == FREE_TEXT:
        names = names[:-1]
    free = values.get(FREE_TEXT, "")

    def at(index: int) -> str:
        """The argument at that index -- 0-based, as Claude Code documents it.

        The declared name at that index, or, with nothing declared, that word
        of the free text: `$0` is the first either way.
        """
        if names:
            return values.get(names[index], "") if index < len(names) else ""
        words = free.split()
        return words[index] if index < len(words) else ""

    def fill(match: re.Match) -> str:
        indexed, positional, name = match.groups()
        if indexed is not None:
            return at(int(indexed))
        if positional is not None:
            # `$N` is shorthand for `$ARGUMENTS[N]`, so the two agree.
            return at(int(positional))
        if name is not None:
            # An undeclared `$dir` is prose, not a placeholder: served as written.
            return values.get(name, match.group(0))
        return free

    filled = TOKEN.sub(fill, body)
    if free and not names and not POSITIONAL.search(body):
        # What Claude Code does with a command whose body never places its
        # arguments: hand them to the model rather than drop them.
        return f"{filled}\n\nARGUMENTS: {free}"
    return filled
