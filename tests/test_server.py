"""The MCP surface: what a client sees, as resources and as the tool mirror."""

import json

import pytest
from fastmcp import Client

from mcp_school import School
from mcp_school.config import Config
from tests.conftest import make_config


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    return result.content[0].text


# -- resources ----------------------------------------------------------------


@pytest.mark.unit
async def test_resources_are_indexes_not_one_per_skill(skills_dir):
    """The listing is the cheap layer; it must not grow with the catalogue."""
    server = School(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        uris = [str(r.uri) for r in await client.list_resources()]
    assert "skill://flatsource" in uris
    assert "skill://plugin-a" in uris
    assert not any("SKILL.md" in u for u in uris)


@pytest.mark.unit
async def test_reading_an_index_then_a_skill(skills_dir):
    """The whole interface: list addresses, read one, read the next."""
    server = School(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        index = (await client.read_resource("skill://plugin-a"))[0].text
        assert "skill://deepsource/gamma" in index
        body = (await client.read_resource("skill://deepsource/gamma"))[0].text
    assert "Third skill." in body


@pytest.mark.unit
async def test_reading_a_manifest_then_one_of_its_files(skills_dir):
    server = School(make_config(skills_dir), skills_dir / "_cache")
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
    server = School(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        with pytest.raises(Exception, match=r"nope|[Uu]nknown|not found"):
            await client.read_resource("skill://flatsource/nope")


@pytest.mark.unit
async def test_server_scoped_to_packs_hides_the_rest(skills_dir):
    """A config that lists less is the hard scope: the other pack is unreachable."""
    server = School(
        make_config(skills_dir, packs=["flatsource"]), skills_dir / "_cache"
    )
    async with Client(server.mcp) as client:
        uris = [str(r.uri) for r in await client.list_resources()]
        assert "skill://deepsource" not in uris
        with pytest.raises(Exception, match=r"nknown|not found"):
            await client.read_resource("skill://deepsource/gamma")


@pytest.mark.unit
async def test_empty_config_still_serves(tmp_path):
    """No sources configured must not crash the server -- it just serves nothing."""
    server = School(Config(sources=[]), tmp_path / "cache")
    async with Client(server.mcp) as client:
        assert await client.list_resources() == []


@pytest.mark.unit
async def test_a_failed_source_does_not_take_the_others_down(skills_dir):
    """One bad source must not stop the rest of the catalogue from loading."""
    raw = {
        "sources": [s.model_dump(mode="json") for s in make_config(skills_dir).sources]
    }
    raw["sources"].append({"name": "gone", "url": "file:///no/such/dir"})
    server = School(Config.model_validate(raw), skills_dir / "_cache")
    assert server.status["gone"]["status"] == "failed"
    assert server.status["flatsource"]["status"] == "ok"
    async with Client(server.mcp) as client:
        uris = [str(r.uri) for r in await client.list_resources()]
    assert "skill://flatsource" in uris


# -- the tool mirror ----------------------------------------------------------


@pytest.mark.unit
async def test_the_mirrors_are_four_tools_whatever_the_catalogue_holds(skills_dir):
    """Adding sources or prompts must never add tools; the mirrors absorb them."""
    server = School(make_config(skills_dir), skills_dir / "_cache")
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
    server = School(make_config(skills_dir), skills_dir / "_cache")
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
    server = School(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        resources = {str(r.uri) for r in await client.list_resources()}
        rows = json.loads(await call(client, "list_resources"))
    assert {row["uri"] for row in rows} == resources
    assert set(rows[0]) == {"uri", "name", "description", "mimeType"}


@pytest.mark.unit
async def test_read_resource_returns_what_reading_the_uri_returns(skills_dir):
    server = School(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        through_tool = await call(
            client, "read_resource", uri="skill://deepsource/gamma"
        )
        through_resource = (await client.read_resource("skill://deepsource/gamma"))[
            0
        ].text
    assert through_tool == through_resource


@pytest.mark.unit
async def test_read_resource_explains_an_unknown_uri(skills_dir):
    """A tool answers with prose; only the resource half raises."""
    server = School(make_config(skills_dir), skills_dir / "_cache")
    async with Client(server.mcp) as client:
        out = await call(client, "read_resource", uri="skill://nope/nope")
    assert "No resource" in out and "list_resources" in out


@pytest.mark.unit
async def test_the_mirror_honours_the_hard_pack_scope(skills_dir):
    """A tool call must not reach past a source that was never configured."""
    server = School(
        make_config(skills_dir, packs=["flatsource"]), skills_dir / "_cache"
    )
    async with Client(server.mcp) as client:
        out = await call(client, "read_resource", uri="skill://deepsource/gamma")
    assert "No resource" in out


async def test_a_configmap_mount_is_served_under_the_names_it_was_mounted_as(tmp_path):
    """Every row names the path the config asked for, never the symlink target.

    A ConfigMap mounts `shared/foo.md -> ..data/shared/foo.md`, and `..data`
    points at a timestamp directory Kubernetes replaces on every update. A row
    recording the resolved path lists `..2026.../shared/foo.md` — not the URI a
    skill cites, and gone after the next update.
    """
    root = tmp_path / "pack"
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
                "name": "pack",
                "url": f"file://{root}",
                "include": {"prompts": [], "files": ["shared/**/*"]},
            }
        ]
    )
    school = School(config, tmp_path / "cache")

    assert school.snapshot.resources.files("pack") == ["shared/foo.md"]
    assert all(".." not in str(s.path) for s in school.index.visible())
    async with Client(school.mcp) as client:
        shared = (await client.read_resource("skill://pack/shared/foo.md"))[0].text
        skill = (await client.read_resource("skill://pack/alpha"))[0].text
    assert shared == "shared body"
    assert "skill body" in skill
