"""Skills discovery and integrity through real MCP requests, in both eras."""

from __future__ import annotations

import hashlib
import json

import frontmatter
import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import Request, RequestParams
from pydantic import ConfigDict, TypeAdapter

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.mcp.skills import EXTENSION_ID
from tests.conftest import make_config
from tests.test_header_scope import server_url  # noqa: F401 - shared HTTP fixture


class ExtensionParams(RequestParams):
    model_config = ConfigDict(extra="allow")


async def request(client, method, **params):
    return await client.session.send_request(
        Request(method=method, params=ExtensionParams(**params)), TypeAdapter(dict)
    )


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_discovery_get_and_verified_reads(skills_dir, mode):
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "cache")
    async with Client(server.mcp, mode=mode) as client:
        caps = client.session.server_capabilities
        assert caps.resources is not None
        assert caps.extensions[EXTENSION_ID] == {}
        listing = await request(client, "skills/list")
        assert listing["resultType"] == "complete"
        assert listing["ttlMs"] == 0
        assert listing["cacheScope"] == "private"
        assert len(listing["skills"]) == 4
        assert len(await client.list_resources()) < len(listing["skills"]) + 4
        assert await client.list_tools() == []
        for entry in listing["skills"]:
            got = await request(client, "skills/get", uri=entry["uri"])
            assert got["skill"] == entry
            assert entry["resources"]
            for resource in entry["resources"]:
                result = await client.read_resource(resource["uri"])
                data = result[0].text.encode("utf-8")
                assert resource["size"] == len(data)
                assert resource["digest"] == f"sha256:{hashlib.sha256(data).hexdigest()}"
                if resource["uri"] == entry["uri"]:
                    assert frontmatter.loads(result[0].text).metadata == entry["frontmatter"]


async def test_manifest_hashes_transformed_wire_bytes_and_preserves_metadata(skills_dir):
    path = skills_dir / "flatsource" / "alpha"
    (path / "SKILL.md").write_bytes(
        b"---\r\nname: alpha\r\ndescription: |\r\n  First line.\r\n"
        b"  Second line.\r\nmetadata:\r\n  audience: [one, two]\r\n---\r\n"
        b"Read ${CLAUDE_SKILL_DIR}/guide.md from ${CLAUDE_PLUGIN_ROOT}.\r\n"
    )
    (path / "guide.md").write_bytes("Café\r\n".encode())
    (path / ".env").write_text("secret", encoding="utf-8")
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "cache")
    async with Client(server.mcp) as client:
        entry = (await request(client, "skills/get", uri="skill://flatsource/alpha/SKILL.md"))["skill"]
        assert entry["frontmatter"]["description"] == "First line.\nSecond line.\n"
        assert entry["frontmatter"]["metadata"] == {"audience": ["one", "two"]}
        assert not any(r["uri"].endswith("/.env") for r in entry["resources"])
        for resource in entry["resources"]:
            text = (await client.read_resource(resource["uri"]))[0].text
            data = text.encode("utf-8")
            assert resource["size"] == len(data)
            assert resource["digest"] == f"sha256:{hashlib.sha256(data).hexdigest()}"
            assert "${CLAUDE_" not in text
        legacy = json.loads((await client.read_resource("skill://flatsource/alpha/_manifest"))[0].text)
        disk_hash = next(f["hash"] for f in legacy["files"] if f["path"] == "SKILL.md")
        wire_hash = next(r["digest"] for r in entry["resources"] if r["uri"] == entry["uri"])
        assert wire_hash != disk_hash


async def test_direct_get_needs_no_listing_and_follows_refresh(skills_dir):
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "cache")
    uri = "skill://flatsource/alpha/SKILL.md"
    async with Client(server.mcp) as client:
        before = (await request(client, "skills/get", uri=uri))["skill"]
        path = skills_dir / "flatsource" / "alpha" / "SKILL.md"
        path.write_text(path.read_text() + "\nUpdated instructions.\n", encoding="utf-8")
        server.refresh(force=True)
        after = (await request(client, "skills/get", uri=uri))["skill"]
        assert before["resources"] != after["resources"]


async def test_unknown_skill_invalid_cursor_and_optional_directory_method(skills_dir):
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "cache")
    async with Client(server.mcp) as client:
        for uri in ("skill://flatsource/nope/SKILL.md", "skill://flatsource/_index.md"):
            with pytest.raises(MCPError) as error:
                await request(client, "skills/get", uri=uri)
            assert error.value.code == -32602
        with pytest.raises(MCPError) as error:
            await request(client, "skills/list", cursor="not-issued")
        assert error.value.code == -32602
        with pytest.raises(MCPError) as error:
            await request(client, "resources/directory/read", uri="skill://flatsource/alpha")
        assert error.value.code == -32601


async def test_malformed_frontmatter_remains_an_ordinary_resource(skills_dir):
    path = skills_dir / "flatsource" / "alpha" / "SKILL.md"
    path.write_text("---\nname: alpha\n---\nMissing description.\n", encoding="utf-8")
    server = KnowledgeBase(make_config(skills_dir), skills_dir / "cache")
    async with Client(server.mcp) as client:
        listing = await request(client, "skills/list")
        assert len(listing["skills"]) == 3
        assert (await client.read_resource("skill://flatsource/alpha/SKILL.md"))[0].text
        with pytest.raises(MCPError) as error:
            await request(client, "skills/get", uri="skill://flatsource/alpha/SKILL.md")
        assert error.value.code == -32602


@pytest.mark.integration
async def test_http_scope_and_header_precedence(server_url):  # noqa: F811
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        f"{server_url}?library=deepsource",
        headers={"X-Skill-Library": "flatsource"},
    )
    async with Client(transport) as client:
        listing = await request(client, "skills/list")
        assert len(listing["skills"]) == 2
        assert all(s["uri"].startswith("skill://flatsource/") for s in listing["skills"])
        errors = []
        for uri in ("skill://deepsource/plugin-a/gamma/SKILL.md", "skill://missing/nope/SKILL.md"):
            with pytest.raises(MCPError) as error:
                await request(client, "skills/get", uri=uri)
            errors.append((error.value.code, error.value.message))
        assert errors == [(-32602, "Skill not found")] * 2
    with pytest.raises(MCPError, match="no such library"):
        async with Client(f"{server_url}?library=missing") as client:
            await request(client, "skills/list")
