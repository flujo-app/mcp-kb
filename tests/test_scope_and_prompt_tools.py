"""Library, category and tag scoping, and the prompt tool mirror, over real HTTP.

Both only exist inside an HTTP request -- a scope is read from the MCP URL or
its headers, and whether a mirror is shown depends on what the client declared
there -- so these run the app on a loopback port, as test_header_scope does.

The catalogue is shaped to make the rules visible: ``observe`` holds two
plugins, one in category ``observability`` tagged ``ops`` with skills and a
prompt, one in category ``tooling`` tagged ``extra`` and ``ops`` with a skill;
``design`` (category ``design``, tagged ``ui``) has skills only; and ``notes``
(tagged ``ops``) has a prompt and no skills at all.
"""

import json
import logging
import threading

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.config import Config
from kubed.mcp_kb.mcp.pins import COMMA_IN_CATEGORY, FOLDER_IN_LIBRARY
from kubed.mcp_kb.mcp.scope import Scope
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
    _skill(base / "observe-extra", "alerts")
    # Up and serving nothing at all: a Nextcloud folder with nothing in it yet.
    (base / "blank").mkdir()
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
            "plugins": [
                {
                    "name": "observe",
                    "source": f"file://{base / 'observe'}",
                    "category": "observability",
                    "tags": ["ops"],
                },
                {
                    "name": "observe-extra",
                    "source": f"file://{base / 'observe-extra'}",
                    "category": "tooling",
                    "tags": ["extra", "ops"],
                },
                {
                    "name": "design",
                    "source": f"file://{base / 'design'}",
                    "category": "design",
                    "tags": ["ui"],
                },
                {
                    "name": "notes",
                    "source": f"file://{base / 'notes'}",
                    "tags": ["ops"],
                    "skills": [],
                    "prompts": ["*.md"],
                },
                {"name": "blank", "source": f"file://{base / 'blank'}"},
                # A library whose only plugin is down: named, and serving nothing.
                {"name": "down", "source": f"file://{base / 'not-there'}"},
            ],
            "libraries": [
                {"name": "observe", "plugins": ["observe", "observe-extra"]},
                {"name": "design", "plugins": ["design"]},
                {"name": "notes", "plugins": ["notes"]},
                {"name": "blank", "plugins": ["blank"]},
                {"name": "down", "plugins": ["down"]},
            ],
        }
    )
    port = _free_port()
    app = KnowledgeBase(config, tmp_path_factory.mktemp("cache")).mcp.http_app()
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


async def _skills(url, query=""):
    async with _client(url, query + "&skills=full") as client:
        return sorted(
            str(r.uri) for r in await client.list_resources()
            if str(r.uri).endswith("/SKILL.md")
        )


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


@pytest.mark.parametrize(
    ("name", "arguments", "says"),
    [
        ("observe_nope", {}, "Unknown prompt: 'observe_nope'. Call list_prompts()"),
        ("observe_debug", {}, "Prompt 'observe_debug' needs the argument app."),
    ],
)
async def test_a_prompt_tool_mistake_is_an_error_result_that_says_what_to_do(
    url, name, arguments, says, caplog
):
    """The caller's mistake: an error result, and nothing in the log above DEBUG."""
    with caplog.at_level(logging.DEBUG, logger="fastmcp"):
        async with _client(url, "?prompts=off") as client:
            result = await client.call_tool(
                "get_prompt",
                {"name": name, "arguments": arguments},
                raise_on_error=False,
            )
    assert result.is_error
    assert result.content[0].text.startswith(says)
    server = [r for r in caplog.records if r.name.startswith("fastmcp.server")]
    assert [r.getMessage() for r in server if r.levelno > logging.DEBUG] == []


# -- library, categories and tags -----------------------------------------------


async def test_a_library_is_the_whole_library(url):
    _, resources, prompts = await _seen(url, "?library=observe")
    assert resources == ["observe/_index.md"]
    assert prompts == ["observe_debug"]
    assert await _skills(url, "?library=observe") == [
        "skill://observe/alerts/SKILL.md",
        "skill://observe/loki/SKILL.md",
        "skill://observe/tempo/SKILL.md",
    ]


async def test_a_library_of_prompts_alone_is_visible_by_name(url):
    _, resources, prompts = await _seen(url, "?library=notes")
    assert resources == []
    assert prompts == ["notes_standup"]


async def test_tags_select_across_libraries(url):
    _, resources, prompts = await _seen(url, "?tags=ops")
    assert resources == ["observe/_index.md"]
    assert prompts == ["notes_standup", "observe_debug"]


async def test_a_repeated_tag_parameter_means_any_of_them(url):
    _, resources, _ = await _seen(url, "?tags=ui&tags=extra")
    assert resources == ["design/_index.md", "observe/_index.md"]
    assert await _skills(url, "?tags=ui&tags=extra") == [
        "skill://design/palette/SKILL.md",
        "skill://observe/alerts/SKILL.md",
    ]


async def test_a_comma_in_a_tag_means_all_of_them(url):
    """`?tags=extra,ops` admits only a plugin carrying both: `observe-extra`,
    not `observe` (ops alone) and not `notes` (ops alone)."""
    _, resources, prompts = await _seen(url, "?tags=extra,ops")
    assert resources == ["observe/_index.md"]
    assert prompts == []
    assert await _skills(url, "?tags=extra,ops") == ["skill://observe/alerts/SKILL.md"]


async def test_a_category_selects_the_plugins_declaring_it(url):
    _, resources, prompts = await _seen(url, "?categories=observability")
    assert resources == ["observe/_index.md"]
    assert prompts == ["observe_debug"]
    assert await _skills(url, "?categories=observability") == [
        "skill://observe/loki/SKILL.md",
        "skill://observe/tempo/SKILL.md",
    ]


async def test_a_repeated_category_parameter_means_any_of_them(url):
    _, resources, _ = await _seen(url, "?categories=observability&categories=design")
    assert resources == ["design/_index.md", "observe/_index.md"]


async def test_tags_narrow_within_a_library(url):
    _, resources, prompts = await _seen(url, "?library=observe&tags=extra")
    assert resources == ["observe/_index.md"]
    assert prompts == []
    assert await _skills(url, "?library=observe&tags=extra") == [
        "skill://observe/alerts/SKILL.md"
    ]


# -- a scope that names nothing --------------------------------------------------

REFUSED = [
    (
        "?library=nope",
        "The scope names library 'nope', and there is no such library."
        " The libraries are: blank, design, down, notes, observe.",
    ),
    ("?tags=opps", "The scope names tag 'opps', which nothing carries. The tags are:"),
    ("?library=observe/loki", FOLDER_IN_LIBRARY),
    ("?library=observe/nope", FOLDER_IN_LIBRARY),
    ("?categories=observability,design", COMMA_IN_CATEGORY),
    (
        "?categories=nope",
        "The scope names category 'nope', which no plugin declares."
        " The categories are: design, observability, tooling.",
    ),
    (
        "?library=observe&categories=design",
        "The scope names category 'design', which no plugin declares."
        " The categories are: observability, tooling.",
    ),
    (
        "?library=observe&tags=ui,design",
        "The scope names tags 'design', 'ui', which nothing carries."
        " The tags are: extra, ops.",
    ),
    (
        # Both exist in the library; no one plugin carries the pair.
        "?library=observe&categories=tooling&tags=ops,ui",
        "The scope names tag 'ui', which nothing carries. The tags are: extra, ops.",
    ),
    (
        # A category one plugin has and a tag another has: nothing carries both.
        "?library=observe&categories=observability&tags=extra",
        "The scope names things that exist, but nothing in library 'observe'"
        " carries the combination: category observability; tags extra.",
    ),
    (
        "?tags=ops,ui",
        "The scope names things that exist, but nothing in any library carries"
        " the combination: tags ops,ui.",
    ),
]


@pytest.mark.parametrize(("query", "says"), REFUSED)
async def test_a_scope_that_names_nothing_is_refused_saying_what_there_is(
    url, query, says
):
    """A typo in a client's config is an error at connect, not an empty server."""
    from mcp.shared.exceptions import MCPError
    from mcp_types import INVALID_PARAMS

    with pytest.raises(MCPError) as caught:
        await _seen(url, query)
    assert caught.value.error.code == INVALID_PARAMS
    assert caught.value.error.message.startswith(says)


async def test_a_library_that_is_down_is_empty_not_refused(url):
    """It exists; it is only serving nothing right now, which /health explains."""
    assert await _seen(url, "?library=down") == ([], [], [])


async def test_a_library_that_is_up_and_empty_is_empty_not_refused(url):
    assert await _seen(url, "?library=blank") == ([], [], [])


@pytest.mark.parametrize("narrower", ["categories", "tags"])
async def test_a_library_that_is_down_carries_no_category_or_tag_to_refuse(
    url, narrower
):
    """The categories and the tags are read off the *snapshot*, because a
    marketplace's are not in the config -- so a library serving nothing has
    none of either, and checking them before the library short-circuit told a
    client pinned to a declared library that its category does not exist.
    """
    assert await _seen(url, f"?library=down&{narrower}=whatever") == ([], [], [])


async def test_a_header_scope_is_refused_the_same_way(url):
    with pytest.raises(Exception, match="no such library"):
        await _seen(url, headers={"X-Skill-Library": "nope"})


async def test_a_header_beats_the_url_parameter(url):
    _, resources, _ = await _seen(url, "?library=design", {"X-Skill-Library": "observe"})
    assert resources == ["observe/_index.md"]


async def test_the_prompt_tools_are_held_to_the_scope(url):
    """The tools route through prompts/list and prompts/get, so the scope binds."""
    async with _client(url, "?prompts=off&library=design") as client:
        listed = json.loads((await client.call_tool("list_prompts", {})).content[0].text)
        with pytest.raises(ToolError, match="observe_debug"):
            await client.call_tool("get_prompt", {"name": "observe_debug"})
    assert listed == []


# -- scope across plugins of one library ------------------------------------------


@pytest.fixture
def mixed(tmp_path):
    """One library fed by two differently-tagged plugins, and a prompts-only
    library named like a folder of another. The shapes the simple fixture
    above cannot express."""
    from kubed.mcp_kb.mcp.prompts import PromptProvider

    def skill(root, *parts):
        d = root.joinpath("skills", *parts)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {parts[-1]}\ndescription: x\n---\nb\n")

    skill(tmp_path / "ops", "loki")
    skill(tmp_path / "ui", "palette")
    (tmp_path / "ui" / "shared").mkdir()
    (tmp_path / "ui" / "shared" / "tokens.md").write_text("ui-only material")
    skill(tmp_path / "alpha", "notes", "x")
    (tmp_path / "alpha" / "prompts").mkdir()
    (tmp_path / "alpha" / "prompts" / "a.md").write_text("---\ndescription: a\n---\nhi\n")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "n.md").write_text("---\ndescription: n\n---\nhi\n")
    config = Config.model_validate(
        {
            "plugins": [
                {"name": "ops", "tags": ["ops"], "source": f"file://{tmp_path / 'ops'}",
                 "prompts": []},
                {"name": "ui", "tags": ["ui"], "source": f"file://{tmp_path / 'ui'}",
                 "prompts": [], "files": ["shared/**/*"]},
                {"name": "alpha", "source": f"file://{tmp_path / 'alpha'}"},
                {"name": "notes", "source": f"file://{tmp_path / 'notes'}",
                 "skills": [], "prompts": ["*.md"]},
            ],
            "libraries": [
                {"name": "obs", "plugins": ["ops", "ui"]},
                {"name": "alpha", "plugins": ["alpha"]},
                {"name": "notes", "plugins": ["notes"]},
            ],
        }
    )
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")
    return knowledge_base, PromptProvider(lambda: knowledge_base.snapshot)


def _tag(name):
    return Scope(tags=frozenset({frozenset({name})}))


def test_a_tag_scope_cannot_read_another_plugins_library_file(mixed):
    """Two plugins feed `obs`; `tokens.md` came from the `ui` one.

    Admitting library files by library alone let an `ops` scope — which does
    see a skill in `obs` — read the `ui` plugin's file by guessing its URI.
    """
    knowledge_base, _ = mixed
    catalogue = knowledge_base.snapshot.catalogue

    assert catalogue.read("skill://obs/shared/tokens.md", _tag("ops")) is None
    assert catalogue.read("skill://obs/shared/tokens.md", _tag("ui")) == "ui-only material"


def test_a_tag_scope_does_not_list_another_plugins_library_files(mixed):
    knowledge_base, _ = mixed
    catalogue = knowledge_base.snapshot.catalogue

    assert catalogue.read("skill://obs/_files.md", _tag("ops")) is None
    assert "obs/_files.md" not in [e.name for e in catalogue.entries(_tag("ops"))]
    assert "tokens.md" in catalogue.read("skill://obs/_files.md", _tag("ui"))


def test_a_prompts_only_library_is_selected_by_its_name_alone(mixed):
    """`notes` is a folder inside `alpha` and also a library holding only prompts.

    Only the library is a selector: `notes` gives the prompts-only library and
    never the folder, and the folder's spelling names nothing at all.
    """
    knowledge_base, provider = mixed

    assert [p.name for p in provider.visible(Scope("notes"))] == ["notes_n"]
    assert knowledge_base.index.visible(Scope("notes")) == []
    assert knowledge_base.index.visible(Scope("alpha/notes")) == []
    assert provider.visible(Scope("alpha/notes")) == []
