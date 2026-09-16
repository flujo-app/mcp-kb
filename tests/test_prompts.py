"""Prompts: the files, how they render, and who may see them."""

import logging
import pathlib

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 only
    import tomli as tomllib

import pytest
from fastmcp import Client

from mcp_school import School, harvest
from mcp_school.config import Include
from mcp_school.prompts import load_prompt, load_prompts
from tests.conftest import load_all_prompts, load_pack_prompts

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


def test_a_missing_directory_is_no_prompts(tmp_path):
    """Harvest yields no files for a missing source; loading an empty list loads none."""
    files = harvest.prompt_files(tmp_path / "nope", Include())
    assert load_prompts(files, pack="flatsource", source="flatsource") == []


# -- over MCP ------------------------------------------------------------------


async def test_a_client_sees_the_prompt_and_its_arguments(skills_dir, prompts_dir):
    async with Client(School(skills_dir, prompts_dir=prompts_dir).mcp) as client:
        listed = {p.name: p for p in await client.list_prompts()}
    hello = listed["flatsource_hello"]
    assert hello.description == "Say hello."
    assert [(a.name, a.required) for a in hello.arguments] == [
        ("who", True),
        ("greeting", False),
    ]


async def test_rendering_fills_arguments_and_defaults(skills_dir, prompts_dir):
    async with Client(School(skills_dir, prompts_dir=prompts_dir).mcp) as client:
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
    async with Client(School(skills_dir, prompts_dir=prompts_dir).mcp) as client:
        with pytest.raises(Exception, match="who"):
            await client.get_prompt("flatsource_hello", {})


async def test_logql_braces_survive_rendering(skills_dir, prompts_dir):
    async with Client(School(skills_dir, prompts_dir=prompts_dir).mcp) as client:
        result = await client.get_prompt("deepsource_check", {"service": "api"})
    assert text(result) == '{app="api"} |= "error"\n'


async def test_skill_packs_scopes_prompts(skills_dir, prompts_dir):
    """A prompt from an unloaded pack is neither listed nor renderable."""
    server = School(skills_dir, packs=["flatsource"], prompts_dir=prompts_dir)
    async with Client(server.mcp) as client:
        assert [p.name for p in await client.list_prompts()] == ["flatsource_hello"]
        with pytest.raises(Exception, match="deepsource_check"):
            await client.get_prompt("deepsource_check", {"service": "api"})


async def test_the_shipped_grafana_prompt_renders(skills_dir):
    async with Client(School(skills_dir, prompts_dir=SHIPPED).mcp) as client:
        result = await client.get_prompt("grafana_debug-logs", {"app": "nextcloud"})
    body = text(result)
    assert "**nextcloud**" in body
    assert "the last 1h" in body and "{{" not in body
