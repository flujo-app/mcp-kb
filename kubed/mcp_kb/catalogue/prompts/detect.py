"""Which dialect a file is written in: strongest signal first (§C1.34).

A duck check on the body alone would misread prose -- a paragraph that
mentions `$5` is not a Claude command -- so the body is the last resort and
only decides between dialects nothing else has ruled on. The one key that says
the file is not a prompt comes before all of it, since every rung below only
answers *which* dialect; then what the config says, then where the file is,
then which frontmatter keys it carries.

Directories are deliberately not looked at here: a `commands/` tree and a
manifest's `commands` entry are Claude's, and the caller that knows about
either passes ``dialect="claude"`` rather than teaching this module about
paths it cannot see.
"""

from __future__ import annotations

from pathlib import Path

from . import claude, copilot, mcpkb

AUTO = "auto"

# VS Code's convention, and the one filename that names its own dialect.
COPILOT_SUFFIX = ".prompt.md"

COPILOT_KEYS = ("agent", "tools")
CLAUDE_KEYS = ("argument-hint",)

# `applyTo` and `globs` say which files some guidance applies to. That is an
# instructions file, and publishing one as a command would offer the model a
# rule to run.
NOT_A_PROMPT = ("applyTo", "globs")

_NAMES = "names"
_OBJECTS = "objects"


def dialect_for(path: Path, meta: dict, body: str, declared: str = AUTO) -> str | None:
    """The dialect's ``NAME``, or None when the file is not a prompt at all."""
    # Above everything, the declaration included: a dialect says how to read a
    # prompt, never whether the file is one. An instructions file that landed
    # in a `commands/` tree, or under a plugin declaring `dialect: claude`, is
    # still a rule for the model to follow -- publishing it as a command is
    # precisely what this key exists to refuse.
    if not_a_prompt(meta):
        return None

    if declared and declared != AUTO:
        return declared

    if path.name.endswith(COPILOT_SUFFIX):
        return copilot.NAME

    # Before the rest of the keys: a VS Code instructions or chatmode file
    # carries `tools:` as well, so reading that key first would decide a
    # dialect for a file the check above has not seen.
    if any(key in meta for key in COPILOT_KEYS):
        return copilot.NAME

    # Read only once the stronger signals have passed, so a file they already
    # answered for is never refused over the shape of a key nothing will read.
    arguments = _arguments(meta.get("arguments"))
    if any(key in meta for key in CLAUDE_KEYS) or arguments == _NAMES:
        return claude.NAME
    if arguments == _OBJECTS:
        return mcpkb.NAME

    if mcpkb.PLACEHOLDER.search(body):
        return mcpkb.NAME
    if copilot.INPUT.search(body):
        return copilot.NAME
    if claude.POSITIONAL.search(body):
        return claude.NAME

    # A description and a body are the same thing in every dialect, so the
    # file is served by the one that adds nothing to them.
    return mcpkb.NAME


def not_a_prompt(meta: dict) -> str | None:
    """The key that makes this file instructions rather than a prompt."""
    return next((key for key in NOT_A_PROMPT if key in meta), None)


def _arguments(raw: object) -> str | None:
    """Whether ``arguments`` is a list of names (Claude's) or of objects (ours).

    An empty list says nothing either way and is treated as absent. A list
    that is half one and half the other is refused rather than guessed at:
    whichever way it were read, half the file's arguments would be wrong.
    """
    if isinstance(raw, str):
        return _NAMES if raw.split() else None
    if not isinstance(raw, list) or not raw:
        return None
    if all(isinstance(item, str) for item in raw):
        return _NAMES
    if all(isinstance(item, dict) for item in raw):
        return _OBJECTS
    raise ValueError("arguments must be names or objects, not both")
