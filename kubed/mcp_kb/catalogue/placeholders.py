"""The `${…}` placeholders this server is the authority on.

Claude's placeholders are client-side machinery -- ``${selection}``,
``${CLAUDE_PROJECT_DIR}``, ``${CLAUDE_SESSION_ID}`` name things only the
client has, and Claude Code itself leaves them literal in a skill it did not
fetch locally (§C1.35 point 7). Two are different: ``${CLAUDE_PLUGIN_ROOT}``
and ``${CLAUDE_SKILL_DIR}`` name *this server's* material, so they become
addresses in the ``skill://`` address space and are always resolved (§C1.37).

Plain ``str.replace``: a regex over `${…}` would have to decide what to do
with every other spelling, and the answer for every other spelling is
"nothing".
"""

from __future__ import annotations

PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
SKILL_DIR = "${CLAUDE_SKILL_DIR}"


def substitute(text: str, *, plugin_root: str, skill_dir: str | None = None) -> str:
    """Replace the placeholders this server can answer; leave every other `${…}` alone.

    ``skill_dir`` is absent for a prompt -- a command is not a skill and has no
    directory of its own -- and ``${CLAUDE_SKILL_DIR}`` then stays as written,
    which is what every other unanswerable placeholder does too.
    """
    text = text.replace(PLUGIN_ROOT, plugin_root)
    if skill_dir is not None:
        text = text.replace(SKILL_DIR, skill_dir)
    return text
