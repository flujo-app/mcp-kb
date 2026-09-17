"""Prompts: the files, how they render, and who may see them."""

import logging
import pathlib
from types import SimpleNamespace

import pytest
from fastmcp import Client

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.catalogue import harvest
from kubed.mcp_kb.catalogue.prompts import FilePrompt, load_prompt, load_prompts
from kubed.mcp_kb.catalogue.skills import SkillIndex
from kubed.mcp_kb.config import Include
from kubed.mcp_kb.mcp.prompts import PromptProvider
from kubed.mcp_kb.mcp.scope import Scope
from tests.conftest import load_all_prompts, load_library_prompts, make_config

pytestmark = pytest.mark.unit


def text(result):
    return result.messages[0].content.text


# -- loading -------------------------------------------------------------------


def test_the_name_is_library_qualified(prompts_dir):
    """A file outside a library folder is not a prompt at all."""
    assert {p.name for p in load_all_prompts(prompts_dir)} == {
        "flatsource_hello",
        "deepsource_check",
    }


def test_libraries_hard_scope_the_prompts(prompts_dir):
    names = {p.name for p in load_library_prompts(prompts_dir, "flatsource")}
    assert names == {"flatsource_hello"}


def test_an_undeclared_placeholder_is_skipped_loudly(tmp_path, caplog):
    """Rendered blank, a typo reads fine and asks the model for the wrong thing."""
    (tmp_path / "flatsource").mkdir()
    typo = tmp_path / "flatsource" / "typo.md"
    typo.write_text(
        "---\ndescription: typo\narguments:\n- name: name\n---\nHi {{ nmae }}\n"
    )
    with caplog.at_level(logging.WARNING):
        assert load_prompts([typo], library="flatsource", source="flatsource") == []
    assert "nmae" in caplog.text


def test_a_required_argument_cannot_have_a_default(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text(
        "---\narguments:\n- name: who\n  required: true\n  default: x\n---\n{{ who }}\n"
    )
    with pytest.raises(ValueError, match="cannot have a default"):
        load_prompt(path, "flatsource")


def test_a_default_empty_source_leaves_no_empty_tag(tmp_path):
    """``source=""`` (the default) must not put an empty-string tag in the set."""
    path = tmp_path / "hello.md"
    path.write_text("---\ndescription: hi\n---\nHi.\n")
    prompt = load_prompt(path, "flatsource")
    assert "" not in prompt.tags
    assert prompt.tags == {"flatsource", "prompt"}


def test_a_missing_directory_is_no_prompts(tmp_path):
    """Harvest yields no files for a missing source; loading an empty list loads none."""
    files = harvest.prompt_files(tmp_path / "nope", Include())
    assert load_prompts(files, library="flatsource", source="flatsource") == []


# -- splitting frontmatter from the body ---------------------------------------


def test_a_body_that_recurs_in_the_frontmatter_is_sliced_by_position(tmp_path):
    """``str.index`` finds the body's first occurrence anywhere, including inside
    the frontmatter block when the description happens to repeat the body text."""
    path = tmp_path / "echo.md"
    path.write_text("---\ndescription: body\n---\nbody\n")
    prompt = load_prompt(path, "flatsource")
    assert prompt.template == "body\n"


def test_an_unterminated_frontmatter_block_is_refused(tmp_path):
    path = tmp_path / "broken.md"
    path.write_text("---\ndescription: d\nbody never closed\n")
    with pytest.raises(ValueError, match="no YAML frontmatter"):
        load_prompt(path, "flatsource")


def test_a_body_starting_with_a_dashed_rule_still_parses(tmp_path):
    """A real frontmatter block, followed by a body that itself starts with
    ``---``, must not be mistaken for "no frontmatter at all"."""
    path = tmp_path / "rule.md"
    path.write_text("---\ndescription: d\n---\n---\nnot frontmatter, just text\n")
    prompt = load_prompt(path, "flatsource")
    assert prompt.template == "---\nnot frontmatter, just text\n"


# -- over MCP ------------------------------------------------------------------


async def test_a_client_sees_the_prompt_and_its_arguments(skills_dir, prompts_dir):
    server = KnowledgeBase(make_config(skills_dir, prompts_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        listed = {p.name: p for p in await client.list_prompts()}
    hello = listed["flatsource_hello"]
    assert hello.description == "Say hello."
    assert [(a.name, a.required) for a in hello.arguments] == [
        ("who", True),
        ("greeting", False),
    ]


async def test_rendering_fills_arguments_and_defaults(skills_dir, prompts_dir):
    server = KnowledgeBase(make_config(skills_dir, prompts_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        given = await client.get_prompt(
            "flatsource_hello", {"who": "Dr K", "greeting": "Hi"}
        )
        defaulted = await client.get_prompt("flatsource_hello", {"who": "Dr K"})
        blank = await client.get_prompt(
            "flatsource_hello", {"who": "Dr K", "greeting": ""}
        )
    assert text(given) == "Hi, Dr K.\n"
    assert text(defaulted) == "Hello, Dr K.\n"
    # A picker sends a field left empty as "", which must not beat the default.
    assert text(blank) == "Hello, Dr K.\n"


async def test_a_missing_required_argument_is_refused(skills_dir, prompts_dir, caplog):
    """Invalid params naming the argument -- the caller's mistake -- and not the
    internal error and ERROR traceback FastMCP gives any other render failure."""
    from mcp.shared.exceptions import MCPError
    from mcp_types import INVALID_PARAMS

    server = KnowledgeBase(make_config(skills_dir, prompts_dir), skills_dir / "_cache")
    with caplog.at_level(logging.INFO):
        async with Client(server.mcp) as client:
            with pytest.raises(MCPError) as caught:
                await client.get_prompt("flatsource_hello", {})
    assert caught.value.error.code == INVALID_PARAMS
    assert caught.value.error.message == "Prompt 'flatsource_hello' needs the argument who."
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


async def test_logql_braces_survive_rendering(skills_dir, prompts_dir):
    server = KnowledgeBase(make_config(skills_dir, prompts_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        result = await client.get_prompt("deepsource_check", {"service": "api"})
    assert text(result) == '{app="api"} |= "error"\n'


async def test_skill_libraries_scopes_prompts(skills_dir, prompts_dir):
    """A prompt from an unconfigured library is neither listed nor renderable."""
    config = make_config(skills_dir, prompts_dir, libraries=["flatsource"])
    server = KnowledgeBase(config, skills_dir / "_cache")
    async with Client(server.mcp) as client:
        assert [p.name for p in await client.list_prompts()] == ["flatsource_hello"]
        with pytest.raises(Exception, match="deepsource_check"):
            await client.get_prompt("deepsource_check", {"service": "api"})


# -- reading the snapshot -------------------------------------------------------


def test_visible_reads_the_snapshot_exactly_once():
    """Two separate reads of `KnowledgeBase.snapshot` could straddle a swap and mix
    generations (M6) -- `PromptProvider` must take one reference and derive
    both the prompts and the index from it, the way `resources.py` and
    `routes.py` already do.
    """
    calls = []
    prompt = FilePrompt(
        path=pathlib.Path("/x/hello.md"),
        name="flatsource_hello",
        library="flatsource",
        source="flatsource",
        template="hi",
    )
    snapshot = SimpleNamespace(prompts=(prompt,), index=SkillIndex([]))

    def snapshot_getter():
        calls.append(1)
        return snapshot

    provider = PromptProvider(snapshot_getter)
    provider.visible(Scope("flatsource"))

    assert len(calls) == 1
