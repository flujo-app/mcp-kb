"""The per-request knobs, exercised over real HTTP.

All three only exist inside an HTTP request, so an in-memory client cannot reach
this code at all: ``X-Skill-Pack`` (which pack), ``?resources=off`` (whether the
mirror is advertised) and ``?skills=full`` (how much the listing enumerates).
These tests run the ASGI app on a real port for that reason.
"""

import json
import socket
import threading
import urllib.request

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.skills import download_skill, get_skill_manifest, list_skills

from mcp_school import School
from tests.conftest import make_config


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url(skills_dir_module, prompts_dir_module):
    """Serve the skills app on a loopback port for the duration of the module."""
    port = _free_port()
    config = make_config(skills_dir_module, prompts_dir_module)
    app = School(config, skills_dir_module / "_cache").mcp.http_app()
    uvicorn_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(uvicorn_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def _client(url, headers=None):
    return Client(StreamableHttpTransport(url, headers=headers or {}))


async def tool_names(url, headers=None):
    async with _client(url, headers) as client:
        return sorted(t.name for t in await client.list_tools())


async def resource_uris(url, headers=None):
    async with _client(url, headers) as client:
        return [str(r.uri) for r in await client.list_resources()]


async def call(url, tool, headers=None, **args):
    async with _client(url, headers) as client:
        return (await client.call_tool(tool, args)).content[0].text


# -- the mirror toggle --------------------------------------------------------


@pytest.mark.integration
async def test_a_resource_client_is_shown_no_tools(server_url):
    """The mirror is noise to a client that can read the real thing."""
    assert await tool_names(server_url) == []


@pytest.mark.integration
async def test_resources_off_reveals_the_mirror(server_url):
    """n8n has no resources, so for n8n the mirror IS the server."""
    assert await tool_names(f"{server_url}?resources=off") == [
        "list_resources",
        "read_resource",
    ]


@pytest.mark.integration
async def test_the_header_declares_it_too(server_url):
    """A header can be set in a credential where a URL cannot."""
    names = await tool_names(server_url, headers={"X-MCP-Resources": "off"})
    assert names == ["list_resources", "read_resource"]


@pytest.mark.integration
async def test_a_hidden_tool_is_still_callable(server_url):
    """Hiding from a listing is presentation; refusing to run would be a
    different and worse contract."""
    out = await call(server_url, "read_resource", uri="skill://flatsource/alpha")
    assert "First skill." in out


# -- the listing shape --------------------------------------------------------


@pytest.mark.integration
async def test_the_listing_is_indexes_by_default(server_url):
    uris = await resource_uris(server_url)
    assert "skill://flatsource" in uris
    assert not any(u.endswith("SKILL.md") for u in uris)


@pytest.mark.integration
async def test_skills_full_enumerates_every_skill(server_url):
    """Skill-syncing clients find skills only by scanning for /SKILL.md."""
    uris = await resource_uris(f"{server_url}?skills=full")
    assert "skill://flatsource/alpha/SKILL.md" in uris
    assert sum(1 for u in uris if u.endswith("/SKILL.md")) == 4


# -- the pack pin -------------------------------------------------------------


@pytest.mark.integration
async def test_no_header_sees_every_pack(server_url):
    uris = await resource_uris(server_url)
    assert "skill://flatsource" in uris and "skill://deepsource" in uris


@pytest.mark.integration
async def test_the_pin_hides_the_other_pack_from_the_listing(server_url):
    uris = await resource_uris(server_url, headers={"X-Skill-Pack": "flatsource"})
    assert uris == ["skill://flatsource"]


@pytest.mark.integration
async def test_the_pin_blocks_reading_another_packs_resource(server_url):
    """The hole this closes: filtering a listing leaves guessable URIs readable,
    and every URI here is guessable by design."""
    async with _client(server_url, {"X-Skill-Pack": "flatsource"}) as client:
        with pytest.raises(Exception, match=r"nknown|not found|deepsource"):
            await client.read_resource("skill://deepsource/gamma")


@pytest.mark.integration
async def test_the_pin_blocks_the_mirror_too(server_url):
    """A scope enforced on only one half of the protocol is not a scope."""
    out = await call(
        f"{server_url}?resources=off",
        "read_resource",
        headers={"X-Skill-Pack": "flatsource"},
        uri="skill://deepsource/gamma",
    )
    assert "No resource" in out


@pytest.mark.integration
async def test_the_pin_narrows_the_mirrors_listing(server_url):
    rows = json.loads(
        await call(
            f"{server_url}?resources=off",
            "list_resources",
            headers={"X-Skill-Pack": "flatsource"},
        )
    )
    assert [row["uri"] for row in rows] == ["skill://flatsource"]


@pytest.mark.integration
async def test_the_pin_still_allows_its_own_pack(server_url):
    async with _client(server_url, {"X-Skill-Pack": "flatsource"}) as client:
        body = (await client.read_resource("skill://flatsource/alpha"))[0].text
    assert "First skill." in body


@pytest.mark.integration
async def test_a_group_pin_cannot_widen_to_the_whole_pack(server_url):
    """Pinning to one group must not hand over its siblings."""
    async with _client(server_url, {"X-Skill-Pack": "plugin-a"}) as client:
        assert (
            "Third skill."
            in (await client.read_resource("skill://deepsource/gamma"))[0].text
        )
        with pytest.raises(Exception, match=r"nknown|not found"):
            await client.read_resource("skill://deepsource/delta")


@pytest.mark.integration
async def test_the_listing_does_not_leak_other_pack_names(server_url):
    """A pinned client must not learn the other packs exist from a listing."""
    uris = await resource_uris(server_url, headers={"X-Skill-Pack": "plugin-a"})
    assert not any("plugin-b" in u or "flatsource" in u for u in uris)


# -- prompts ------------------------------------------------------------------


async def prompt_names(url, headers=None):
    async with _client(url, headers) as client:
        return sorted(p.name for p in await client.list_prompts())


@pytest.mark.integration
async def test_the_pin_scopes_prompts_too(server_url):
    """Out of the pinned pack, a prompt is neither listed nor renderable."""
    pinned = {"X-Skill-Pack": "flatsource"}
    assert await prompt_names(server_url, pinned) == ["flatsource_hello"]
    async with _client(server_url, pinned) as client:
        with pytest.raises(Exception, match="deepsource_check"):
            await client.get_prompt("deepsource_check", {"service": "api"})


@pytest.mark.integration
async def test_a_group_pin_sees_its_packs_prompts(server_url):
    """A prompt belongs to a pack, as pack-level files do."""
    assert await prompt_names(server_url, {"X-Skill-Pack": "plugin-a"}) == [
        "deepsource_check"
    ]


# -- syncing skills to disk ---------------------------------------------------


@pytest.mark.integration
async def test_fastmcp_syncs_skills_from_the_full_listing(
    server_url, skills_dir_module, tmp_path
):
    """The promise ?skills=full exists to keep, checked with FastMCP's own helpers."""
    async with _client(f"{server_url}?skills=full") as client:
        found = {s.name for s in await list_skills(client)}
        manifest = await get_skill_manifest(client, "flatsource/alpha")
        downloaded = await download_skill(client, "flatsource/alpha", tmp_path)
    assert found == {
        "flatsource/alpha",
        "flatsource/beta",
        "deepsource/gamma",
        "deepsource/delta",
    }
    assert "SKILL.md" in [f.path for f in manifest.files]
    source = skills_dir_module / "flatsource" / "alpha" / "SKILL.md"
    assert (downloaded / "SKILL.md").read_text() == source.read_text()


@pytest.mark.integration
async def test_the_default_listing_gives_sync_helpers_nothing(server_url):
    """Why the flag exists: the cheap listing has no /SKILL.md rows to find."""
    async with _client(server_url) as client:
        assert await list_skills(client) == []


# -- /health --------------------------------------------------------------------


@pytest.mark.integration
def test_health_reports_the_catalogue(server_url):
    """/health is plain HTTP outside the MCP protocol, so it is fetched directly."""
    url = server_url.removesuffix("/mcp") + "/health"
    with urllib.request.urlopen(url) as response:
        body = json.loads(response.read())
    assert body["status"] == "ok"
    assert sorted(body["libraries"]) == ["deepsource", "flatsource"]
    assert body["skills"] == 4
    assert body["prompts"] == 2
    flatsource = body["sources"]["flatsource"]
    assert body["generation"] == 0
    assert {
        k: flatsource[k] for k in ("status", "library", "skills", "prompts", "files")
    } == {
        "status": "ok",
        "library": "flatsource",
        "skills": 2,
        "prompts": 0,
        "files": 0,
    }
    assert flatsource["built"] and flatsource["fingerprint"]["files"] == 3
    assert body["sources"]["deepsource-prompts"]["status"] == "ok"
