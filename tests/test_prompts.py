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
from kubed.mcp_kb.config import Config
from kubed.mcp_kb.mcp.prompts import PromptProvider
from kubed.mcp_kb.mcp.scope import Scope
from kubed.mcp_kb.plugins import Globs
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
        assert load_prompts([typo], library="flatsource", plugin="flatsource") == []
    assert "nmae" in caplog.text


def test_a_required_argument_cannot_have_a_default(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text(
        "---\narguments:\n- name: who\n  required: true\n  default: x\n---\n{{ who }}\n"
    )
    with pytest.raises(ValueError, match="cannot have a default"):
        load_prompt(path, "flatsource")


def test_a_prompt_carries_its_plugins_labels_and_nothing_implicit(tmp_path):
    """No library name, plugin name or "prompt" smuggled in as a tag."""
    path = tmp_path / "hello.md"
    path.write_text("---\ndescription: hi\n---\nHi.\n")
    bare = load_prompt(path, "flatsource")
    assert bare.tags == set()
    labelled = load_prompt(
        path, "flatsource", plugin="p", tags=["ops", "lgtm"], category="obs"
    )
    assert labelled.tags == {"ops", "lgtm"}
    assert labelled.category == "obs"
    assert labelled.plugin == "p"


def test_a_missing_directory_is_no_prompts(tmp_path):
    """Harvest yields no files for a missing source; loading an empty list loads none."""
    files = harvest.prompt_files(tmp_path / "nope", Globs())
    assert load_prompts(files, library="flatsource", plugin="flatsource") == []


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
    caplog.clear()
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


async def test_a_claude_command_and_a_copilot_prompt_serve_as_one_kind_of_prompt(
    tmp_path,
):
    """The dialect seam, end to end over a real client: a Claude Code command
    under ``commands/`` and a VS Code ``.prompt.md`` list beside this server's
    own dialect and render with their own placeholders filled. A Copilot
    ``${input:…}`` name is also an MCP argument name, so it has to survive the
    client's argument schema on the way round.
    """
    root = tmp_path / "plugin"
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "deploy.md").write_text(
        "---\ndescription: Deploy an app.\narguments: [app, env]\n---\n"
        "Deploy $app to $env.\n"
    )
    (root / ".github" / "prompts").mkdir(parents=True)
    (root / ".github" / "prompts" / "review.prompt.md").write_text(
        "---\ndescription: Review a file.\n---\n"
        "Review ${input:target:Which file?} for ${input:concern}.\n"
    )
    config = Config.model_validate(
        {
            "plugins": [{"name": "kit", "source": f"file://{root}"}],
            "libraries": [{"name": "kit", "plugins": ["kit"]}],
        }
    )
    server = KnowledgeBase(config, tmp_path / "cache")
    async with Client(server.mcp) as client:
        listed = {p.name: p for p in await client.list_prompts()}
        deployed = await client.get_prompt("kit_deploy", {"app": "api", "env": "prod"})
        reviewed = await client.get_prompt(
            "kit_review", {"target": "main.py", "concern": "races"}
        )
    assert [a.name for a in listed["kit_deploy"].arguments] == ["app", "env"]
    assert listed["kit_review"].arguments[0].description == "Which file?"
    assert [a.name for a in listed["kit_review"].arguments] == ["target", "concern"]
    assert text(deployed) == "Deploy api to prod.\n"
    assert text(reviewed) == "Review main.py for races.\n"
    assert {p.dialect for p in server.prompts} == {"claude", "copilot"}


async def test_the_index_row_decides_a_prompts_dialect(tmp_path):
    """The dialect a row records is the authority at snapshot time, not the
    path rule: a command under `commands/` whose row says `copilot` is read as
    Copilot on the next start, `${input:app}` becoming its one argument."""
    import json

    root = tmp_path / "plugin"
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "deploy.md").write_text(
        "---\ndescription: Deploy.\narguments: [env]\n---\n"
        "Deploy ${input:app} to $env.\n"
    )
    config = Config.model_validate(
        {
            "plugins": [{"name": "kit", "source": f"file://{root}"}],
            "libraries": [{"name": "kit", "plugins": ["kit"]}],
        }
    )
    cache = tmp_path / "cache"
    harvested = KnowledgeBase(config, cache)
    assert [(p.dialect, [a.name for a in p.arguments]) for p in harvested.prompts] == [
        ("claude", ["env"])
    ]

    index = json.loads((cache / "index.json").read_text())
    (row,) = index["plugins"]["kit"]["prompts"]
    row["dialect"] = "copilot"
    (cache / "index.json").write_text(json.dumps(index))
    restarted = KnowledgeBase(config, cache)

    (prompt,) = restarted.prompts
    assert prompt.dialect == "copilot"
    assert [a.name for a in prompt.arguments] == ["app"]
    async with Client(restarted.mcp) as client:
        rendered = await client.get_prompt("kit_deploy", {"app": "api"})
    assert text(rendered) == "Deploy api to $env.\n"


async def test_a_prompts_title_is_published(tmp_path):
    """FastMCP's ``Prompt`` has a title, and a file that declares one gets it."""
    root = tmp_path / "plugin"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "hello.md").write_text(
        "---\ntitle: Say Hello\ndescription: Greets.\n---\nHi.\n"
    )
    config = Config.model_validate(
        {
            "plugins": [{"name": "kit", "source": f"file://{root}"}],
            "libraries": [{"name": "kit", "plugins": ["kit"]}],
        }
    )
    server = KnowledgeBase(config, tmp_path / "cache")
    async with Client(server.mcp) as client:
        listed = {p.name: p for p in await client.list_prompts()}
    assert listed["kit_hello"].title == "Say Hello"


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
        plugin="flatsource",
        template="hi",
    )
    snapshot = SimpleNamespace(prompts=(prompt,), index=SkillIndex([]))

    def snapshot_getter():
        calls.append(1)
        return snapshot

    provider = PromptProvider(snapshot_getter)
    provider.visible(Scope("flatsource"))

    assert len(calls) == 1
