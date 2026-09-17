"""The placeholders this server can answer, and the ones it must leave alone.

Two of Claude's placeholders name this server's own material -- the plugin's
files and a skill's own directory -- so they become addresses in the address
space. Every other `${…}` names something only the client has, and is served
as written.
"""

import pytest

from kubed.mcp_kb.catalogue import placeholders
from kubed.mcp_kb.catalogue.prompts import load_prompt

pytestmark = pytest.mark.unit

DIALECTS = ["mcp-kb", "claude", "copilot"]


def write(path, frontmatter, body):
    path.write_text(f"---\n{frontmatter}---\n{body}")
    return path


def test_both_placeholders_this_server_knows_are_replaced():
    text = "${CLAUDE_PLUGIN_ROOT}/shared/x.md and ${CLAUDE_SKILL_DIR}/SKILL.md"
    assert (
        placeholders.substitute(
            text, plugin_root="skill://lib", skill_dir="skill://lib/a/b"
        )
        == "skill://lib/shared/x.md and skill://lib/a/b/SKILL.md"
    )


def test_a_placeholder_only_the_client_can_fill_is_untouched():
    text = "${CLAUDE_PROJECT_DIR}/x ${CLAUDE_SESSION_ID} ${selection}"
    assert placeholders.substitute(text, plugin_root="skill://lib") == text


def test_without_a_skill_dir_the_skill_placeholder_stays_literal():
    """A prompt is not a skill, so nothing hands it a skill directory."""
    text = "${CLAUDE_SKILL_DIR}/SKILL.md"
    assert placeholders.substitute(text, plugin_root="skill://lib") == text


@pytest.mark.parametrize("dialect", DIALECTS)
def test_rendering_resolves_the_plugin_root_in_every_dialect(tmp_path, dialect):
    path = write(
        tmp_path / f"{dialect}.md",
        "description: d\n",
        "Read ${CLAUDE_PLUGIN_ROOT}/shared/x.md.",
    )
    prompt = load_prompt(path, "lib", dialect=dialect)
    assert prompt.render(plugin_root="skill://lib") == "Read skill://lib/shared/x.md."
    # No plugin root to substitute is the loader's own case: the text stands.
    assert prompt.render() == "Read ${CLAUDE_PLUGIN_ROOT}/shared/x.md."
