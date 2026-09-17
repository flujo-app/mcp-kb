"""The MCP surface: what a client sees, as resources and as the tool mirror."""

import json
import logging

import pytest
from fastmcp import Client

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.config import Config
from tests.conftest import make_config


async def call(client, name, **args):
    """The tool's text, whether it is a result or an error result."""
    result = await client.call_tool(name, args, raise_on_error=False)
    return result.content[0].text


# -- resources ----------------------------------------------------------------


@pytest.mark.unit
async def test_resources_are_indexes_not_one_per_skill(skills_dir):
    """The listing is the cheap layer; it must not grow with the catalogue."""
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        uris = [str(r.uri) for r in await client.list_resources()]
    assert "skill://flatsource/_index.md" in uris
    assert "skill://deepsource/plugin-a/_index.md" in uris
    assert not any("SKILL.md" in u for u in uris)


@pytest.mark.unit
async def test_reading_an_index_then_a_skill(skills_dir):
    """The whole interface: list addresses, read one, read the next."""
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        index = (await client.read_resource("skill://deepsource/plugin-a/_index.md"))[
            0
        ].text
        assert "skill://deepsource/plugin-a/gamma/SKILL.md" in index
        body = (
            await client.read_resource("skill://deepsource/plugin-a/gamma/SKILL.md")
        )[0].text
    assert "Third skill." in body


@pytest.mark.unit
async def test_reading_a_manifest_then_one_of_its_files(skills_dir):
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        manifest = (await client.read_resource("skill://flatsource/alpha/_manifest"))[
            0
        ].text
        paths = [f["path"] for f in json.loads(manifest)["files"]]
        assert "SKILL.md" in paths
        body = (await client.read_resource("skill://flatsource/alpha/SKILL.md"))[0].text
    assert "First skill." in body


@pytest.mark.unit
async def test_an_unknown_uri_is_an_error_not_an_empty_read(skills_dir):
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        with pytest.raises(Exception, match=r"nope|[Uu]nknown|not found"):
            await client.read_resource("skill://flatsource/nope")


@pytest.mark.unit
async def test_server_scoped_to_libraries_hides_the_rest(skills_dir):
    """A config that lists less is the hard scope: the other library is unreachable."""
    server = KnowledgeBase(
        make_config(skills_dir, libraries=["flatsource"]), skills_dir / "_cache"
    )
    async with Client(server.mcp) as client:
        uris = [str(r.uri) for r in await client.list_resources()]
        assert not any(u.startswith("skill://deepsource/") for u in uris)
        with pytest.raises(Exception, match=r"nknown|not found"):
            await client.read_resource("skill://deepsource/plugin-a/gamma/SKILL.md")


@pytest.mark.unit
async def test_empty_config_still_serves(tmp_path):
    """No sources configured must not crash the server -- it just serves nothing."""
    server = KnowledgeBase(Config(sources=[]), tmp_path / "cache")
    async with Client(server.mcp) as client:
        assert await client.list_resources() == []


@pytest.mark.unit
async def test_a_failed_source_does_not_take_the_others_down(skills_dir):
    """One bad source must not stop the rest of the catalogue from loading."""
    raw = {
        "sources": [s.model_dump(mode="json") for s in make_config(skills_dir).sources]
    }
    raw["sources"].append({"name": "gone", "url": "file:///no/such/dir"})
    server = KnowledgeBase(Config.model_validate(raw), skills_dir / "_cache")
    assert server.status["gone"]["status"] == "failed"
    assert server.status["flatsource"]["status"] == "ok"
    async with Client(server.mcp) as client:
        uris = [str(r.uri) for r in await client.list_resources()]
    assert "skill://flatsource/_index.md" in uris


# -- the tool mirror ----------------------------------------------------------


@pytest.mark.unit
async def test_the_mirrors_are_four_tools_whatever_the_catalogue_holds(skills_dir):
    """Adding sources or prompts must never add tools; the mirrors absorb them."""
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    # Skip the middleware, which is the thing that hides these. What is
    # registered is what an ?resources=off client is shown; what a resource
    # client sees is covered over real HTTP in test_header_scope.
    registered = await server.mcp.list_tools(run_middleware=False)
    assert sorted(t.name for t in registered) == [
        "get_prompt",
        "list_prompts",
        "list_resources",
        "read_resource",
    ]


@pytest.mark.unit
async def test_the_mirror_tools_are_advertised_as_read_only(skills_dir):
    """Unannotated, MCP's defaults advertise a tool as destructive."""
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    registered = await server.mcp.list_tools(run_middleware=False)
    assert registered
    for tool in registered:
        hints = tool.to_mcp_tool().annotations
        assert hints is not None, f"{tool.name} has no annotations"
        assert hints.read_only_hint is True
        assert hints.destructive_hint is False
        assert hints.idempotent_hint is True
        assert hints.open_world_hint is False
        assert hints.title


@pytest.mark.unit
async def test_the_mirror_returns_the_same_rows_as_the_resource_listing(skills_dir):
    """If these drift, the tool is no longer the resource interface."""
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        resources = {str(r.uri) for r in await client.list_resources()}
        rows = json.loads(await call(client, "list_resources"))
    assert {row["uri"] for row in rows} == resources
    assert set(rows[0]) == {"uri", "name", "description", "mimeType"}


@pytest.mark.unit
async def test_read_resource_returns_what_reading_the_uri_returns(skills_dir):
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        gamma = "skill://deepsource/plugin-a/gamma/SKILL.md"
        through_tool = await call(client, "read_resource", uri=gamma)
        through_resource = (await client.read_resource(gamma))[0].text
    assert through_tool == through_resource


@pytest.mark.unit
async def test_read_resource_explains_an_unknown_uri(skills_dir, caplog):
    """An error result, so a caller branching on isError sees a miss -- with
    what to do next in its message, and no traceback in the server's log."""
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "_cache")
    with caplog.at_level(logging.INFO):
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "read_resource", {"uri": "skill://nope/nope"}, raise_on_error=False
            )
    assert result.is_error
    out = result.content[0].text
    assert "No resource" in out and "list_resources" in out
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


@pytest.mark.unit
async def test_the_mirror_honours_the_hard_library_scope(skills_dir):
    """A tool call must not reach past a source that was never configured."""
    server = KnowledgeBase(
        make_config(skills_dir, libraries=["flatsource"]), skills_dir / "_cache"
    )
    async with Client(server.mcp) as client:
        out = await call(
            client, "read_resource", uri="skill://deepsource/plugin-a/gamma/SKILL.md"
        )
    assert "No resource" in out


async def test_a_configmap_mount_is_served_under_the_names_it_was_mounted_as(tmp_path):
    """Every row names the path the config asked for, never the symlink target.

    A ConfigMap mounts `shared/foo.md -> ..data/shared/foo.md`, and `..data`
    points at a timestamp directory Kubernetes replaces on every update. A row
    recording the resolved path lists `..2026.../shared/foo.md` — not the URI a
    skill cites, and gone after the next update.
    """
    root = tmp_path / "library"
    stamp = root / "..2026_09_16_13_15_49"
    (stamp / "shared").mkdir(parents=True)
    (stamp / "shared" / "foo.md").write_text("shared body")
    (stamp / "skills" / "alpha").mkdir(parents=True)
    (stamp / "skills" / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: a\n---\nskill body"
    )
    (root / "..data").symlink_to(stamp)
    (root / "shared").symlink_to(root / "..data" / "shared")
    (root / "skills").symlink_to(root / "..data" / "skills")
    config = Config(
        sources=[
            {
                "name": "library",
                "url": f"file://{root}",
                "include": {"prompts": [], "files": ["shared/**/*"]},
            }
        ]
    )
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")

    assert knowledge_base.snapshot.resources.files("library") == ["shared/foo.md"]
    assert all(".." not in str(s.path) for s in knowledge_base.index.visible())
    async with Client(knowledge_base.mcp) as client:
        shared = (await client.read_resource("skill://library/shared/foo.md"))[0].text
        skill = (await client.read_resource("skill://library/alpha/SKILL.md"))[0].text
    assert shared == "shared body"
    assert "skill body" in skill


@pytest.mark.unit
async def test_a_hidden_file_in_a_skill_is_not_readable_by_guessing(tmp_path):
    """The manifest leaves hidden files out, so a read must too.

    A `.env` beside a SKILL.md is exactly the file a manifest hides and a caller
    should never get by typing its name — through the resource or the mirror.
    """
    skill = tmp_path / "src" / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\nbody\n")
    (skill / ".env").write_text("SECRET=hunter2\n")
    (skill / "notes.md").write_text("visible\n")
    config = Config.model_validate(
        {"sources": [{"name": "lib", "url": f"file://{tmp_path / 'src'}"}]}
    )
    server = KnowledgeBase(config, tmp_path / "cache")

    async with Client(server.mcp) as client:
        visible = (await client.read_resource("skill://lib/x/notes.md"))[0].text
        with pytest.raises(Exception, match=r"[Uu]nknown|not found"):
            await client.read_resource("skill://lib/x/.env")
        mirrored = await call(client, "read_resource", uri="skill://lib/x/.env")

    assert visible == "visible\n"
    assert "hunter2" not in mirrored
