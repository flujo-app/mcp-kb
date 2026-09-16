"""Prompts: the files, how they render, and who may see them."""

import logging
import pathlib
from types import SimpleNamespace

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 only
    import tomli as tomllib

import pytest
from fastmcp import Client

from mcp_school import School, harvest
from mcp_school.config import Include
from mcp_school.prompts import FilePrompt, PromptProvider, load_prompt, load_prompts
from mcp_school.skills import SkillIndex
from tests.conftest import load_all_prompts, load_pack_prompts, make_config

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parent.parent
SHIPPED = REPO / "prompts"


def text(result):
    return result.messages[0].content.text


# -- the shipped prompts -------------------------------------------------------


def test_every_shipped_prompt_loads():
    """Strict here, lenient at runtime.

    The server logs and skips a broken prompt so it cannot take the skills down
    with it, which means a typo in a shipped one would otherwise ship silently.
    """
    paths = sorted(SHIPPED.glob("*/*.md"))
    assert paths, "no shipped prompts — this test proves nothing"
    for path in paths:
        load_prompt(path, path.parent.name)


def test_every_shipped_prompt_belongs_to_a_real_pack():
    """The folder is the pack, and a pack is a skills.toml source."""
    sources = {
        s["name"] for s in tomllib.loads((REPO / "skills.toml").read_text())["source"]
    }
    folders = {p.parent.name for p in SHIPPED.glob("*/*.md")}
    assert folders <= sources, f"prompt folders with no pack: {folders - sources}"


# -- loading -------------------------------------------------------------------


def test_the_name_is_pack_qualified(prompts_dir):
    """A file outside a pack folder is not a prompt at all."""
    assert {p.name for p in load_all_prompts(prompts_dir)} == {
        "flatsource_hello",
        "deepsource_check",
    }


def test_packs_hard_scope_the_prompts(prompts_dir):
    names = {p.name for p in load_pack_prompts(prompts_dir, "flatsource")}
    assert names == {"flatsource_hello"}


def test_an_undeclared_placeholder_is_skipped_loudly(tmp_path, caplog):
    """Rendered blank, a typo reads fine and asks the model for the wrong thing."""
    (tmp_path / "flatsource").mkdir()
    typo = tmp_path / "flatsource" / "typo.md"
    typo.write_text(
        "---\ndescription: typo\narguments:\n- name: name\n---\nHi {{ nmae }}\n"
    )
    with caplog.at_level(logging.WARNING):
        assert load_prompts([typo], pack="flatsource", source="flatsource") == []
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
    assert load_prompts(files, pack="flatsource", source="flatsource") == []


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
    server = School(make_config(skills_dir, prompts_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        listed = {p.name: p for p in await client.list_prompts()}
    hello = listed["flatsource_hello"]
    assert hello.description == "Say hello."
    assert [(a.name, a.required) for a in hello.arguments] == [
        ("who", True),
        ("greeting", False),
    ]


async def test_rendering_fills_arguments_and_defaults(skills_dir, prompts_dir):
    server = School(make_config(skills_dir, prompts_dir), skills_dir / "_cache")
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


async def test_a_missing_required_argument_is_refused(skills_dir, prompts_dir):
    server = School(make_config(skills_dir, prompts_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        with pytest.raises(Exception, match="who"):
            await client.get_prompt("flatsource_hello", {})


async def test_logql_braces_survive_rendering(skills_dir, prompts_dir):
    server = School(make_config(skills_dir, prompts_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        result = await client.get_prompt("deepsource_check", {"service": "api"})
    assert text(result) == '{app="api"} |= "error"\n'


async def test_skill_packs_scopes_prompts(skills_dir, prompts_dir):
    """A prompt from an unconfigured pack is neither listed nor renderable."""
    config = make_config(skills_dir, prompts_dir, packs=["flatsource"])
    server = School(config, skills_dir / "_cache")
    async with Client(server.mcp) as client:
        assert [p.name for p in await client.list_prompts()] == ["flatsource_hello"]
        with pytest.raises(Exception, match="deepsource_check"):
            await client.get_prompt("deepsource_check", {"service": "api"})


async def test_the_shipped_grafana_prompt_renders(skills_dir):
    server = School(make_config(skills_dir, SHIPPED), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        result = await client.get_prompt("grafana_debug-logs", {"app": "nextcloud"})
    body = text(result)
    assert "**nextcloud**" in body
    assert "the last 1h" in body and "{{" not in body


# -- reading the snapshot -------------------------------------------------------


def test_visible_reads_the_snapshot_exactly_once():
    """Two separate reads of `School.snapshot` could straddle a swap and mix
    generations (M6) -- `PromptProvider` must take one reference and derive
    both the prompts and the index from it, the way `resources.py` and
    `routes.py` already do.
    """
    calls = []
    prompt = FilePrompt(
        path=pathlib.Path("/x/hello.md"),
        name="flatsource_hello",
        pack="flatsource",
        source="flatsource",
        template="hi",
    )
    snapshot = SimpleNamespace(prompts=(prompt,), index=SkillIndex([]))

    def snapshot_getter():
        calls.append(1)
        return snapshot

    provider = PromptProvider(snapshot_getter)
    provider.visible("flatsource")

    assert len(calls) == 1
