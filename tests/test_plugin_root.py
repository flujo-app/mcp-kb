"""The plugin root as the fallback ceiling, read back through a running server.

A skill kit factors shared material up out of its skills and cites it from the
plugin root -- ``${CLAUDE_PLUGIN_ROOT}/shared/x.md`` in Claude Code, and
``shared/x.md`` written as if the reader were standing at the repository root.
Neither address is a harvested library file, and both have to read: at the
skill, because that is where a relative citation resolves, and at the library,
because that is what the plugin root's own placeholder becomes.

What the fallback must *not* reach is the point of the rest of this module: a
dotfile, a path that leaves the root, and a file inside a skill's own directory,
whose address is the skill's.

Scope only exists inside an HTTP request, so this runs against a real server on
a loopback port, like ``test_address_space.py``.
"""

import threading

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.config import Config
from tests.test_header_scope import _free_port

pytestmark = pytest.mark.integration


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _build(base):
    kit = base / "kit"
    _write(
        kit,
        "skills/writing/SKILL.md",
        "---\nname: writing\ndescription: Write things.\n---\n\nSee shared/x.md.\n",
    )
    _write(kit, "skills/writing/notes.md", "skill notes\n")
    _write(
        kit,
        "skills/reading/SKILL.md",
        "---\nname: reading\ndescription: Read things.\n---\n\nSibling.\n",
    )
    # Harvested as a skill and then skipped: `Upper` is not one URI segment.
    _write(
        kit,
        "skills/Upper/SKILL.md",
        "---\nname: Upper\ndescription: Skipped.\n---\n\nUNSERVED SKILL\n",
    )
    _write(kit, "skills/Upper/leak.md", "UNSERVED FILE\n")
    _write(kit, "shared/x.md", "shared bytes\n")
    _write(kit, "shared/.env", "SHARED_SECRET=1\n")
    _write(kit, "docs/guide.md", "the guide\n")
    _write(base, "outside.md", "not in any plugin\n")
    (kit / "shared" / "escape.md").symlink_to(base / "outside.md")

    extra = base / "extra"
    _write(
        extra,
        "skills/ops/SKILL.md",
        "---\nname: ops\ndescription: Run things.\n---\n\nBody.\n",
    )
    _write(extra, "ops-notes.md", "ops notes\n")

    return Config.model_validate(
        {
            "plugins": [
                {
                    "name": "kit",
                    "source": f"file://{kit}",
                    "tags": ["core"],
                    "files": ["docs/**"],
                },
                {
                    "name": "kit-extra",
                    "source": f"file://{extra}",
                    "tags": ["extra"],
                    "files": [],
                },
            ],
            "libraries": [{"name": "kit", "plugins": ["kit", "kit-extra"]}],
        }
    )


@pytest.fixture(scope="module")
def url(tmp_path_factory):
    base = tmp_path_factory.mktemp("plugin-root")
    app = KnowledgeBase(_build(base), base / "_cache").mcp.http_app()
    port = _free_port()
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


def _client(url, tags=None):
    headers = {"X-Skill-Tags": tags} if tags else {}
    return Client(StreamableHttpTransport(url, headers=headers))


async def _read(url, uri, tags=None):
    async with _client(url, tags) as client:
        return (await client.read_resource(uri))[0].text


async def _tool(url, uri, tags=None):
    """The mirror's answer, as text, whether it is a body or an error result."""
    async with _client(url, tags) as client:
        result = await client.call_tool(
            "read_resource", {"uri": uri}, raise_on_error=False
        )
    return result.content[0].text


async def _missing(url, uri, tags=None):
    """Whether both surfaces report nothing at ``uri``."""
    async with _client(url, tags) as client:
        with pytest.raises(Exception) as caught:
            await client.read_resource(uri)
        mirrored = await client.call_tool(
            "read_resource", {"uri": uri}, raise_on_error=False
        )
    return "not found" in str(caught.value) and mirrored.is_error


SHARED = "shared bytes\n"


async def test_a_file_at_the_plugin_root_reads_at_the_skill_and_at_the_library(url):
    """The two addresses a kit's own text points at, both answered by one file."""
    for uri in ("skill://kit/writing/shared/x.md", "skill://kit/shared/x.md"):
        assert await _read(url, uri) == SHARED
        assert await _tool(url, uri) == SHARED


async def test_the_files_listing_names_only_the_harvested_files(url):
    """The fallback reads are unlisted: ``files:`` is still what ``_files.md`` is."""
    listing = await _read(url, "skill://kit/_files.md")
    assert "skill://kit/docs/guide.md" in listing
    assert "shared/x.md" not in listing
    assert await _read(url, "skill://kit/docs/guide.md") == "the guide\n"


async def test_a_hidden_file_at_the_plugin_root_is_not_readable_either_way(url):
    for uri in ("skill://kit/shared/.env", "skill://kit/writing/shared/.env"):
        assert await _missing(url, uri)
        assert "SHARED_SECRET" not in await _tool(url, uri)


async def test_a_symlink_out_of_the_plugin_root_is_refused(url):
    for uri in ("skill://kit/shared/escape.md", "skill://kit/writing/shared/escape.md"):
        assert await _missing(url, uri)
        assert "not in any plugin" not in await _tool(url, uri)


async def test_a_path_inside_a_skill_is_refused_at_the_library_base(url):
    """That address is the skill's, and the skill is where it is answered."""
    assert await _read(url, "skill://kit/writing/notes.md") == "skill notes\n"
    for uri in (
        "skill://kit/skills/writing/notes.md",
        "skill://kit/skills/writing/SKILL.md",
    ):
        assert await _missing(url, uri)
        assert "skill notes" not in await _tool(url, uri)


async def test_a_sibling_skills_file_is_not_readable_through_this_skill(url):
    """One file, one address. The plugin root holds the other skills too, and
    reaching one through a sibling's address would serve its bytes twice --
    unsubstituted the second time. A real sibling citation is
    ``../reading/SKILL.md``, which is the sibling's own address by the time a
    read happens."""
    assert "Sibling." in await _read(url, "skill://kit/reading/SKILL.md")
    alias = "skill://kit/writing/skills/reading/SKILL.md"
    assert await _missing(url, alias)
    assert "Sibling." not in await _tool(url, alias)


async def test_a_skipped_skills_files_are_not_readable_through_a_served_one(url):
    """A skill the snapshot would not serve is still a skill directory: its
    files are nobody's to read, at any address."""
    assert await _missing(url, "skill://kit/Upper/SKILL.md")
    for rel in ("skills/Upper/SKILL.md", "skills/Upper/leak.md"):
        for uri in (f"skill://kit/{rel}", f"skill://kit/writing/{rel}"):
            assert await _missing(url, uri)
            assert "UNSERVED" not in await _tool(url, uri)


async def test_the_plugin_roots_are_tried_in_the_librarys_order(url):
    """A library is fed by several plugins, so the fallback walks all of them."""
    assert await _read(url, "skill://kit/ops-notes.md") == "ops notes\n"


async def test_a_scope_that_excludes_the_plugin_reaches_nothing_under_its_root(url):
    assert await _read(url, "skill://kit/shared/x.md", tags="core") == SHARED
    assert await _missing(url, "skill://kit/shared/x.md", tags="extra")
    assert await _missing(url, "skill://kit/writing/shared/x.md", tags="extra")
    assert await _read(url, "skill://kit/ops-notes.md", tags="extra") == "ops notes\n"
    assert await _missing(url, "skill://kit/ops-notes.md", tags="core")
