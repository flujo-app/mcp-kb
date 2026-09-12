"""The MCP surface: what a client sees, as resources and as the tool mirror."""

import json

import pytest
from fastmcp import Client

from kubed.skills_mcp import SkillsMCP, load_skills


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    return result.content[0].text


@pytest.mark.unit
def test_pack_is_the_source_and_group_is_its_parent(skills_dir):
    """A flat source's group collapses onto the pack; a nested one does not."""
    by_name = {s.name: s for s in load_skills(skills_dir)}
    assert (by_name["alpha"].pack, by_name["alpha"].group) == ("flatsource", "flatsource")
    assert (by_name["gamma"].pack, by_name["gamma"].group) == ("deepsource", "plugin-a")


@pytest.mark.unit
def test_load_skills_can_hard_scope_to_packs(skills_dir):
    names = {s.name for s in load_skills(skills_dir, packs=["flatsource"])}
    assert names == {"alpha", "beta"}


@pytest.mark.unit
def test_skills_at_any_depth_are_loaded(skills_dir):
    """n8n nests one level, grafana two; neither layout is hard-coded."""
    assert {s.name for s in load_skills(skills_dir)} == {
        "alpha",
        "beta",
        "gamma",
        "delta",
    }


# -- resources ----------------------------------------------------------------


@pytest.mark.unit
async def test_resources_are_indexes_not_one_per_skill(skills_dir):
    """The listing is the cheap layer; it must not grow with the catalogue."""
    async with Client(SkillsMCP(skills_dir).mcp) as client:
        uris = [str(r.uri) for r in await client.list_resources()]
    assert "skill://flatsource" in uris
    assert "skill://plugin-a" in uris
    assert not any("SKILL.md" in u for u in uris)


@pytest.mark.unit
async def test_reading_an_index_then_a_skill(skills_dir):
    """The whole interface: list addresses, read one, read the next."""
    async with Client(SkillsMCP(skills_dir).mcp) as client:
        index = (await client.read_resource("skill://plugin-a"))[0].text
        assert "skill://deepsource/gamma" in index
        body = (await client.read_resource("skill://deepsource/gamma"))[0].text
    assert "Third skill." in body


@pytest.mark.unit
async def test_reading_a_manifest_then_one_of_its_files(skills_dir):
    async with Client(SkillsMCP(skills_dir).mcp) as client:
        manifest = (
            await client.read_resource("skill://flatsource/alpha/_manifest")
        )[0].text
        paths = [f["path"] for f in json.loads(manifest)["files"]]
        assert "SKILL.md" in paths
        body = (
            await client.read_resource("skill://flatsource/alpha/SKILL.md")
        )[0].text
    assert "First skill." in body


@pytest.mark.unit
async def test_an_unknown_uri_is_an_error_not_an_empty_read(skills_dir):
    async with Client(SkillsMCP(skills_dir).mcp) as client:
        with pytest.raises(Exception, match="nope|[Uu]nknown|not found"):
            await client.read_resource("skill://flatsource/nope")


@pytest.mark.unit
async def test_server_scoped_to_packs_hides_the_rest(skills_dir):
    """SKILL_PACKS is the hard scope: the other pack is not reachable at all."""
    async with Client(SkillsMCP(skills_dir, packs=["flatsource"]).mcp) as client:
        uris = [str(r.uri) for r in await client.list_resources()]
        assert "skill://deepsource" not in uris
        with pytest.raises(Exception):
            await client.read_resource("skill://deepsource/gamma")


@pytest.mark.unit
async def test_empty_skills_dir_still_serves(tmp_path):
    """No skills mounted must not crash the server -- it just serves nothing."""
    async with Client(SkillsMCP(tmp_path).mcp) as client:
        assert await client.list_resources() == []


# -- the tool mirror ----------------------------------------------------------


@pytest.mark.unit
async def test_the_mirror_is_two_tools_whatever_the_catalogue_holds(skills_dir):
    """Adding packs must never add tools; the address space absorbs them."""
    server = SkillsMCP(skills_dir)
    # Skip the middleware, which is the thing that hides these. What is
    # registered is what an ?resources=off client is shown; what a resource
    # client sees is covered over real HTTP in test_header_scope.
    registered = await server.mcp.list_tools(run_middleware=False)
    assert sorted(t.name for t in registered) == ["list_resources", "read_resource"]


@pytest.mark.unit
async def test_the_mirror_returns_the_same_rows_as_the_resource_listing(skills_dir):
    """If these drift, the tool is no longer the resource interface."""
    async with Client(SkillsMCP(skills_dir).mcp) as client:
        resources = {str(r.uri) for r in await client.list_resources()}
        rows = json.loads(await call(client, "list_resources"))
    assert {row["uri"] for row in rows} == resources
    assert set(rows[0]) == {"uri", "name", "description", "mimeType"}


@pytest.mark.unit
async def test_read_resource_returns_what_reading_the_uri_returns(skills_dir):
    async with Client(SkillsMCP(skills_dir).mcp) as client:
        through_tool = await call(client, "read_resource", uri="skill://deepsource/gamma")
        through_resource = (
            await client.read_resource("skill://deepsource/gamma")
        )[0].text
    assert through_tool == through_resource


@pytest.mark.unit
async def test_read_resource_explains_an_unknown_uri(skills_dir):
    """A tool answers with prose; only the resource half raises."""
    async with Client(SkillsMCP(skills_dir).mcp) as client:
        out = await call(client, "read_resource", uri="skill://nope/nope")
    assert "No resource" in out and "list_resources" in out


@pytest.mark.unit
async def test_the_mirror_honours_the_hard_pack_scope(skills_dir):
    """A tool call must not reach past SKILL_PACKS any more than a read does."""
    async with Client(SkillsMCP(skills_dir, packs=["flatsource"]).mcp) as client:
        out = await call(client, "read_resource", uri="skill://deepsource/gamma")
    assert "No resource" in out
