"""Library and tag scoping, and the prompt tool mirror, over real HTTP.

Both only exist inside an HTTP request -- a scope is read from the MCP URL or
its headers, and whether a mirror is shown depends on what the client declared
there -- so these run the app on a loopback port, as test_header_scope does.

The catalogue is shaped to make the rules visible: ``observe`` (tagged ``ops``)
has skills and a prompt, ``design`` (tagged ``ui``) has skills only, and
``notes`` (tagged ``ops``) has a prompt and no skills at all.
"""

import json
import threading

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from mcp_school import School
from mcp_school.config import Config
from mcp_school.scope import Scope
from tests.test_header_scope import _free_port

pytestmark = pytest.mark.unit

MIRRORS = ["get_prompt", "list_prompts", "list_resources", "read_resource"]


def _skill(root, name):
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}.\n---\nbody\n")


@pytest.fixture(scope="module")
def url(tmp_path_factory):
    base = tmp_path_factory.mktemp("scoped")
    for name in ("loki", "tempo"):
        _skill(base / "observe", name)
    _skill(base / "design", "palette")
    (base / "observe" / "prompts").mkdir()
    (base / "observe" / "prompts" / "debug.md").write_text(
        "---\ndescription: Debug a workload.\narguments:\n"
        "- name: app\n  description: The workload.\n  required: true\n"
        "---\nLook at **{{app}}**.\n"
    )
    (base / "notes").mkdir()
    (base / "notes" / "standup.md").write_text("---\ndescription: Stand up.\n---\nGo.\n")
    config = Config.model_validate(
        {
            "libraries": [
                {"name": "observe", "tags": ["ops"]},
                {"name": "design", "tags": ["ui"]},
                {"name": "notes", "tags": ["ops"]},
            ],
            "sources": [
                {"name": "observe", "url": f"file://{base / 'observe'}"},
                {"name": "design", "url": f"file://{base / 'design'}"},
                {
                    "name": "notes",
                    "url": f"file://{base / 'notes'}",
                    "include": {"skills": [], "prompts": ["*.md"]},
                },
            ],
        }
    )
    port = _free_port()
    app = School(config, tmp_path_factory.mktemp("cache")).mcp.http_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def _client(url, query="", headers=None):
    return Client(StreamableHttpTransport(url + query, headers=headers or {}))


async def _seen(url, query="", headers=None):
    """(tool names, index resource names, prompt names) for one client."""
    async with _client(url, query, headers) as client:
        tools = sorted(t.name for t in await client.list_tools())
        resources = sorted(r.name for r in await client.list_resources())
        prompts = sorted(p.name for p in await client.list_prompts())
    return tools, resources, prompts


# -- the prompt mirror ----------------------------------------------------------


async def test_a_client_with_both_native_features_sees_no_mirror_tools(url):
    tools, _, _ = await _seen(url)
    assert tools == []


async def test_prompts_off_shows_only_the_prompt_tools(url):
    tools, _, _ = await _seen(url, "?prompts=off")
    assert tools == ["get_prompt", "list_prompts"]


async def test_both_off_shows_every_mirror(url):
    tools, _, _ = await _seen(url, "?prompts=off&resources=off")
    assert tools == MIRRORS


async def test_list_prompts_gives_each_prompts_arguments(url):
    async with _client(url, "?prompts=off") as client:
        result = await client.call_tool("list_prompts", {})
    listed = {p["name"]: p for p in json.loads(result.content[0].text)}
    assert set(listed) == {"observe_debug", "notes_standup"}
    assert listed["observe_debug"]["arguments"] == [
        {"name": "app", "description": "The workload.", "required": True}
    ]


async def test_get_prompt_returns_rendered_role_tagged_messages(url):
    async with _client(url, "?prompts=off") as client:
        result = await client.call_tool(
            "get_prompt", {"name": "observe_debug", "arguments": {"app": "nextcloud"}}
        )
    messages = json.loads(result.content[0].text)["messages"]
    assert [(m["role"], m["content"].strip()) for m in messages] == [
        ("user", "Look at **nextcloud**.")
    ]


# -- library and tags -----------------------------------------------------------


async def test_a_library_is_the_whole_library(url):
    _, resources, prompts = await _seen(url, "?library=observe")
    assert resources == ["observe"]
    assert prompts == ["observe_debug"]


async def test_a_library_of_prompts_alone_is_visible_by_name(url):
    _, resources, prompts = await _seen(url, "?library=notes")
    assert resources == []
    assert prompts == ["notes_standup"]


async def test_tags_select_across_libraries(url):
    _, resources, prompts = await _seen(url, "?tags=ops")
    assert resources == ["observe"]
    assert prompts == ["notes_standup", "observe_debug"]


async def test_several_tags_mean_any_of_them(url):
    _, resources, _ = await _seen(url, "?tags=ops,ui")
    assert resources == ["design", "observe"]


async def test_tags_narrow_within_a_library(url):
    _, resources, prompts = await _seen(url, "?library=observe&tags=ui")
    assert resources == []
    assert prompts == []


async def test_a_header_beats_the_url_parameter(url):
    _, resources, _ = await _seen(url, "?library=design", {"X-Skill-Library": "observe"})
    assert resources == ["observe"]


async def test_the_prompt_tools_are_held_to_the_scope(url):
    """The tools route through prompts/list and prompts/get, so the scope binds."""
    async with _client(url, "?prompts=off&library=design") as client:
        listed = json.loads((await client.call_tool("list_prompts", {})).content[0].text)
        with pytest.raises(ToolError, match="observe_debug"):
            await client.call_tool("get_prompt", {"name": "observe_debug"})
    assert listed == []


# -- scope across sources and shared group names --------------------------------


@pytest.fixture
def mixed(tmp_path):
    """One library fed by two differently-tagged sources, and two libraries that
    share a group name. The shapes the simple fixture above cannot express."""
    from mcp_school.prompts import PromptProvider

    def skill(root, *parts):
        d = root.joinpath("skills", *parts)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {parts[-1]}\ndescription: x\n---\nb\n")

    skill(tmp_path / "ops", "loki")
    skill(tmp_path / "ui", "palette")
    (tmp_path / "ui" / "shared").mkdir()
    (tmp_path / "ui" / "shared" / "tokens.md").write_text("ui-only material")
    for lib in ("alpha", "beta"):
        skill(tmp_path / lib, "core", f"{lib}-skill")
        (tmp_path / lib / "prompts").mkdir()
        (tmp_path / lib / "prompts" / "p.md").write_text("---\ndescription: p\n---\nhi\n")
    config = Config.model_validate(
        {
            "libraries": [{"name": "obs"}, {"name": "alpha"}, {"name": "beta"}],
            "sources": [
                {"name": "ops", "library": "obs", "tags": ["ops"],
                 "url": f"file://{tmp_path / 'ops'}", "include": {"prompts": []}},
                {"name": "ui", "library": "obs", "tags": ["ui"],
                 "url": f"file://{tmp_path / 'ui'}",
                 "include": {"prompts": [], "files": ["shared/**/*"]}},
                {"name": "alpha", "url": f"file://{tmp_path / 'alpha'}"},
                {"name": "beta", "url": f"file://{tmp_path / 'beta'}"},
            ],
        }
    )
    school = School(config, tmp_path / "cache")
    return school, PromptProvider(lambda: school.snapshot)


def test_a_tag_scope_cannot_read_another_sources_pack_file(mixed):
    """Two sources feed `obs`; `tokens.md` came from the `ui` one.

    Admitting pack files by library alone let an `ops` scope — which does see a
    skill in `obs` — read the `ui` source's file by guessing its URI.
    """
    school, _ = mixed
    catalogue = school.snapshot.catalogue
    ops, ui = Scope(tags=frozenset({"ops"})), Scope(tags=frozenset({"ui"}))

    assert catalogue.read("skill://obs/shared/tokens.md", ops) is None
    assert catalogue.read("skill://obs/shared/tokens.md", ui) == "ui-only material"


def test_a_tag_scope_does_not_list_another_sources_pack_files(mixed):
    school, _ = mixed
    catalogue = school.snapshot.catalogue
    ops, ui = Scope(tags=frozenset({"ops"})), Scope(tags=frozenset({"ui"}))

    assert catalogue.read("skill://obs/_files", ops) is None
    assert "obs/_files" not in [e.name for e in catalogue.entries(ops)]
    assert "tokens.md" in catalogue.read("skill://obs/_files", ui)


def test_a_group_name_shared_by_two_libraries_selects_both_libraries_prompts(mixed):
    """Group names are not unique. Resources admitted every library holding a
    `core` group; prompts stopped at the first one found."""
    school, prompts = mixed
    core = Scope("core")

    assert sorted({s.pack for s in school.index.visible(core)}) == ["alpha", "beta"]
    assert sorted(p.name for p in prompts.visible(core)) == ["alpha_p", "beta_p"]


def test_a_selector_naming_a_group_and_a_prompts_only_library_selects_both(tmp_path):
    """`notes` is a group inside `alpha` and also a library holding only prompts.

    Resources read the selector both ways; prompts treated the library name as a
    fallback for when no group matched, so the prompts-only `notes` vanished.
    """
    from mcp_school.prompts import PromptProvider

    d = tmp_path / "alpha" / "skills" / "notes" / "x"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\nb\n")
    (tmp_path / "alpha" / "prompts").mkdir()
    (tmp_path / "alpha" / "prompts" / "a.md").write_text("---\ndescription: a\n---\nhi\n")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "n.md").write_text("---\ndescription: n\n---\nhi\n")
    config = Config.model_validate(
        {
            "sources": [
                {"name": "alpha", "url": f"file://{tmp_path / 'alpha'}"},
                {"name": "notes", "url": f"file://{tmp_path / 'notes'}",
                 "include": {"skills": [], "prompts": ["*.md"]}},
            ]
        }
    )
    school = School(config, tmp_path / "cache")

    visible = PromptProvider(lambda: school.snapshot).visible(Scope("notes"))

    assert sorted(p.name for p in visible) == ["alpha_a", "notes_n"]
