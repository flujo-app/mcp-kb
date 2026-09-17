"""The ``skill://`` grammar, read back through a running server.

The tree is shaped like the grafana library upstream: skills two levels down
under folders (``skills/<folder>/<skill>/SKILL.md``), a skill directly in the
skill root, a skill nested deeper still, two skills sharing a name in different
folders, a library-level file, and a prompt. A second source feeds the same
library, and a second library reuses a folder and a skill name, which are the
shapes scope and the listing have to keep apart.

Every address is read over MCP from a real ``KnowledgeBase`` on a loopback
port, because scope and the ``?skills=full`` listing exist only inside an HTTP
request.
"""

import json
import posixpath
import re
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


def _skill(root, rel, name, description, body="Body.\n"):
    _write(
        root,
        f"{rel}/SKILL.md",
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}",
    )


def _build(base):
    grafana = base / "grafana"
    _skill(
        grafana,
        "skills/grafana-lgtm/loki",
        "loki",
        "Query logs.",
        "Traces are in [tempo](../tempo/SKILL.md).\n",
    )
    _write(grafana, "skills/grafana-lgtm/loki/references/LOGQL.md", "logql notes\n")
    _write(grafana, "skills/grafana-lgtm/loki/.env", "LOKI_SECRET=1\n")
    _skill(grafana, "skills/grafana-lgtm/tempo", "tempo", "Query traces.")
    _skill(grafana, "skills/grafana-lgtm/testing", "testing", "Test the stack.")
    _skill(grafana, "skills/grafana-k6/k6", "k6", "Load test.")
    _write(grafana, "skills/grafana-k6/k6/SETUP.md", "k6 setup\n")
    _skill(grafana, "skills/grafana-k6/testing", "testing", "Test with k6.")
    _skill(grafana, "skills/overview", "overview", "Start here.")
    _skill(grafana, "skills/grafana-plugins/app/sdk", "sdk", "Build a plugin.")
    _write(grafana, "shared/style.md", "house style\n")
    _write(grafana, "prompts/debug.md", "---\ndescription: Debug.\n---\nGo.\n")

    extra = base / "extra"
    _skill(extra, "skills/grafana-ops/runbook", "runbook", "Run the book.")
    _write(extra, "extra/notes.md", "ops notes\n")
    _write(extra, "prompts/ops.md", "---\ndescription: Ops.\n---\nGo.\n")

    other = base / "other"
    _skill(other, "skills/grafana-lgtm/loki", "loki", "Another loki.")

    handbook = base / "handbook"
    _write(handbook, "guide/intro.md", "welcome\n")

    return Config.model_validate(
        {
            "sources": [
                {
                    "name": "grafana",
                    "url": f"file://{grafana}",
                    "include": {"files": ["shared/**"]},
                },
                {
                    "name": "grafana-extra",
                    "library": "grafana",
                    "url": f"file://{extra}",
                    "include": {"files": ["extra/**"]},
                },
                {"name": "other", "url": f"file://{other}"},
                {
                    "name": "handbook",
                    "url": f"file://{handbook}",
                    "include": {"skills": [], "prompts": [], "files": ["guide/**"]},
                },
            ]
        }
    )


@pytest.fixture(scope="module")
def url(tmp_path_factory):
    base = tmp_path_factory.mktemp("address-space")
    config = _build(base)
    app = KnowledgeBase(config, base / "_cache").mcp.http_app()
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


def _client(url, query="", library=None):
    headers = {"X-Skill-Library": library} if library else {}
    return Client(StreamableHttpTransport(url + query, headers=headers))


async def _read(url, uri, library=None):
    async with _client(url, library=library) as client:
        return (await client.read_resource(uri))[0].text


async def _read_error(url, uri, library=None):
    async with _client(url, library=library) as client:
        with pytest.raises(Exception) as caught:
            await client.read_resource(uri)
    return str(caught.value)


async def _raw(url, uri, library=None):
    """``resources/read`` with ``uri`` sent exactly as given.

    ``Client.read_resource`` passes the URI through pydantic's ``AnyUrl``,
    which resolves ``.`` and ``..`` before it leaves the client -- so it can
    never show what a client that sends the raw string gets. The session
    underneath takes a plain ``str`` and sends it as written, over the same
    HTTP connection.
    """
    async with _client(url, library=library) as client:
        try:
            result = await client.session.read_resource(uri)
        except Exception as exc:  # noqa: BLE001 - the error text is the result
            return f"error: {exc}"
    return result.contents[0].text


async def _tool(url, uri, library=None):
    async with _client(url, library=library) as client:
        result = await client.call_tool("read_resource", {"uri": uri})
    return result.content[0].text


async def _rows(url, query="", library=None):
    async with _client(url, query, library) as client:
        return [
            (str(r.uri), r.name, r.description, r.mime_type)
            for r in await client.list_resources()
        ]


LOKI = "skill://grafana/grafana-lgtm/loki"


# -- the grammar ------------------------------------------------------------------


async def test_a_skill_two_folders_down_is_served_at_its_real_path(url):
    assert "Traces are in" in await _read(url, f"{LOKI}/SKILL.md")
    assert await _read(url, f"{LOKI}/references/LOGQL.md") == "logql notes\n"
    manifest = json.loads(await _read(url, f"{LOKI}/_manifest"))
    assert manifest["skill"] == "grafana/grafana-lgtm/loki"
    assert {f["path"] for f in manifest["files"]} == {"SKILL.md", "references/LOGQL.md"}
    assert await _read(url, "skill://grafana/grafana-k6/k6/SETUP.md") == "k6 setup\n"
    assert "Start here." in await _read(url, "skill://grafana/overview/SKILL.md")
    assert "Build a plugin." in await _read(
        url, "skill://grafana/grafana-plugins/app/sdk/SKILL.md"
    )


async def test_a_sibling_reference_resolves_to_the_siblings_uri(url):
    """Relative references resolve against the skill's root, as a path would."""
    body = await _read(url, f"{LOKI}/SKILL.md")
    reference = re.search(r"\]\(([^)]+)\)", body).group(1)
    resolved = "skill://" + posixpath.normpath(
        posixpath.join(LOKI.removeprefix("skill://"), reference)
    )

    assert resolved == "skill://grafana/grafana-lgtm/tempo/SKILL.md"
    assert "Query traces." in await _read(url, resolved)


async def test_two_skills_with_one_name_in_different_folders_are_both_served(url):
    lgtm = await _read(url, "skill://grafana/grafana-lgtm/testing/SKILL.md")
    k6 = await _read(url, "skill://grafana/grafana-k6/testing/SKILL.md")
    assert "Test the stack." in lgtm
    assert "Test with k6." in k6
    other = await _read(url, "skill://other/grafana-lgtm/loki/SKILL.md")
    assert "Another loki." in other


async def test_a_library_level_file_keeps_its_path_from_the_source_root(url):
    assert await _read(url, "skill://grafana/shared/style.md") == "house style\n"
    assert await _read(url, "skill://grafana/extra/notes.md") == "ops notes\n"


# -- dot segments ---------------------------------------------------------------
#
# A skill links a sibling as `../other/SKILL.md`, and a client that resolves
# that against the skill's URI without normalising sends the `..` as written.
# The server resolves it itself, so every one of these holds through the raw
# resource read and through the tool, which never normalise.

SIBLING = "skill://grafana/grafana-lgtm/loki/../tempo/SKILL.md"


async def test_a_sibling_link_with_dot_segments_reads_the_sibling(url):
    assert "Query traces." in await _raw(url, SIBLING)
    assert "Query traces." in await _tool(url, SIBLING)
    assert "Query traces." in await _raw(
        url, "skill://grafana/./grafana-lgtm/./tempo/SKILL.md"
    )


async def test_dot_segments_clamp_at_the_library_and_never_leave_it(url):
    """`..` past the library's root is dropped, as RFC 3986 does, never popped
    into the authority: the library is the first thing in the URI, not a path."""
    escape = "skill://other/../../grafana-lgtm/loki/SKILL.md"
    assert "Another loki." in await _raw(url, escape)
    assert "Another loki." in await _tool(url, escape)
    across = "skill://other/../grafana/grafana-lgtm/loki/SKILL.md"
    assert "Query logs." not in await _raw(url, across)
    assert "Query logs." not in await _tool(url, across)


async def test_a_dot_segment_read_out_of_scope_reads_as_a_missing_one(url):
    sneak = "skill://grafana/grafana-k6/../grafana-lgtm/loki/SKILL.md"
    missing = "skill://grafana/grafana-k6/nope/SKILL.md"
    assert "Query logs." in await _raw(url, sneak)
    pinned = "grafana/grafana-k6"
    hidden = await _raw(url, sneak, library=pinned)
    assert hidden == (await _raw(url, missing, library=pinned)).replace(missing, sneak)
    hidden = await _tool(url, sneak, library=pinned)
    assert hidden == (await _tool(url, missing, library=pinned)).replace(
        missing, sneak
    )


async def test_a_hidden_file_reached_through_dot_segments_is_still_refused(url):
    for uri in (f"{LOKI}/../loki/.env", f"{LOKI}/references/../.env"):
        assert "LOKI_SECRET" not in await _raw(url, uri)
        assert "LOKI_SECRET" not in await _tool(url, uri)


async def test_a_dot_segment_address_of_a_directory_names_the_file_to_read(url):
    uri = "skill://grafana/grafana-k6/../grafana-lgtm/loki"
    assert f"{LOKI}/SKILL.md" in await _raw(url, uri)
    assert f"{LOKI}/SKILL.md" in await _tool(url, uri)


# -- directories ----------------------------------------------------------------

DIRECTORIES = [
    ("skill://grafana", "skill://grafana/_index.md"),
    ("skill://grafana/", "skill://grafana/_index.md"),
    ("skill://grafana/grafana-lgtm", "skill://grafana/grafana-lgtm/_index.md"),
    ("skill://grafana/grafana-plugins", "skill://grafana/_index.md"),
    ("skill://grafana/shared", "skill://grafana/_files.md"),
    ("skill://handbook", "skill://handbook/_files.md"),
    (LOKI, f"{LOKI}/SKILL.md"),
]


@pytest.mark.parametrize(("uri", "instead"), DIRECTORIES)
async def test_a_directory_is_not_found_and_names_the_file_to_read(url, uri, instead):
    error = await _read_error(url, uri)
    assert "not found" in error
    assert instead in error
    mirrored = await _tool(url, uri)
    assert mirrored.startswith(f"No resource at '{uri}'.")
    assert instead in mirrored


async def test_a_skill_root_names_its_manifest_too(url):
    assert f"{LOKI}/_manifest" in await _read_error(url, LOKI)
    assert f"{LOKI}/_manifest" in await _tool(url, LOKI)


async def test_a_missing_address_names_no_file(url):
    error = await _read_error(url, "skill://grafana/nope")
    assert "not found" in error and "_index.md" not in error
    assert "list_resources" in await _tool(url, "skill://grafana/nope")


OUT_OF_SCOPE = [
    # (a directory unscoped, a missing address beside it, a pin that hides it)
    ("skill://grafana/grafana-lgtm", "skill://grafana/nope", "grafana/grafana-k6"),
    (
        "skill://grafana/grafana-lgtm/loki",
        "skill://grafana/grafana-lgtm/nope",
        "grafana/grafana-k6",
    ),
    ("skill://grafana/extra", "skill://grafana/nope", "grafana/grafana-lgtm"),
    ("skill://grafana", "skill://nope", "other"),
    ("skill://handbook", "skill://nope", "other"),
]


@pytest.mark.parametrize(("uri", "missing", "library"), OUT_OF_SCOPE)
async def test_an_out_of_scope_directory_reads_as_a_missing_one(
    url, uri, missing, library
):
    """The hint must not confirm a directory the caller was not given.

    Unpinned, each address gets a hint a missing one does not, so the case is
    live; pinned, the whole message matches the missing address's, through the
    resource and through the tool.
    """
    unpinned = await _read_error(url, uri)
    assert unpinned != (await _read_error(url, missing)).replace(missing, uri)

    hidden = await _read_error(url, uri, library=library)
    absent = await _read_error(url, missing, library=library)
    assert hidden == absent.replace(missing, uri)

    hidden = await _tool(url, uri, library=library)
    absent = await _tool(url, missing, library=library)
    assert hidden == absent.replace(missing, uri)


async def test_only_a_not_found_read_becomes_a_directory_hint(tmp_path):
    """A directory address that fails some other way keeps its own error."""
    from fastmcp.server.middleware import Middleware

    class Explodes(Middleware):
        async def on_read_resource(self, context, call_next):
            from fastmcp.exceptions import ResourceError

            raise ResourceError("the backend exploded")

    knowledge_base = KnowledgeBase(_build(tmp_path), tmp_path / "_cache")
    knowledge_base.mcp.add_middleware(Explodes())
    async with Client(knowledge_base.mcp) as client:
        with pytest.raises(Exception) as caught:
            await client.read_resource("skill://grafana")
    assert "the backend exploded" in str(caught.value)
    assert "_index.md" not in str(caught.value)


# -- indexes and the listing ------------------------------------------------------


async def test_the_listing_is_exactly_the_index_rows(url):
    md = "text/markdown"
    assert await _rows(url) == [
        (
            "skill://grafana/_index.md",
            "grafana/_index.md",
            "The grafana library — 8 skills in 4 folders:"
            " grafana-k6, grafana-lgtm, grafana-ops, grafana-plugins/app.",
            md,
        ),
        (
            "skill://grafana/grafana-k6/_index.md",
            "grafana/grafana-k6/_index.md",
            "2 skills in the grafana-k6 folder of the grafana library.",
            md,
        ),
        (
            "skill://grafana/grafana-lgtm/_index.md",
            "grafana/grafana-lgtm/_index.md",
            "3 skills in the grafana-lgtm folder of the grafana library.",
            md,
        ),
        (
            "skill://grafana/grafana-ops/_index.md",
            "grafana/grafana-ops/_index.md",
            "1 skill in the grafana-ops folder of the grafana library.",
            md,
        ),
        (
            "skill://grafana/grafana-plugins/app/_index.md",
            "grafana/grafana-plugins/app/_index.md",
            "1 skill in the grafana-plugins/app folder of the grafana library.",
            md,
        ),
        (
            "skill://grafana/_files.md",
            "grafana/_files.md",
            "Files the grafana library ships outside any skill,"
            " which its skills reference.",
            md,
        ),
        (
            "skill://handbook/_files.md",
            "handbook/_files.md",
            "Files the handbook library ships outside any skill,"
            " which its skills reference.",
            md,
        ),
        (
            "skill://other/_index.md",
            "other/_index.md",
            "The other library — 1 skill in 1 folder: grafana-lgtm.",
            md,
        ),
        (
            "skill://other/grafana-lgtm/_index.md",
            "other/grafana-lgtm/_index.md",
            "1 skill in the grafana-lgtm folder of the other library.",
            md,
        ),
    ]


async def test_every_listed_row_reads(url):
    for uri, *_ in await _rows(url, "?skills=full"):
        assert await _read(url, uri)


async def test_the_library_index_lists_every_skill_in_it(url):
    assert await _read(url, "skill://grafana/_index.md") == (
        "# grafana — 8 skills\n\n"
        "skill://grafana/grafana-k6/k6/SKILL.md: Load test.\n"
        "skill://grafana/grafana-k6/testing/SKILL.md: Test with k6.\n"
        "skill://grafana/grafana-lgtm/loki/SKILL.md: Query logs.\n"
        "skill://grafana/grafana-lgtm/tempo/SKILL.md: Query traces.\n"
        "skill://grafana/grafana-lgtm/testing/SKILL.md: Test the stack.\n"
        "skill://grafana/grafana-ops/runbook/SKILL.md: Run the book.\n"
        "skill://grafana/grafana-plugins/app/sdk/SKILL.md: Build a plugin.\n"
        "skill://grafana/overview/SKILL.md: Start here.\n"
        "\nRead any URI above for that skill's instructions. Read _manifest in"
        " place of SKILL.md to see what else it ships, then read one of those"
        " paths under the same skill.\n"
    )


async def test_a_folder_index_lists_only_the_skills_directly_in_it(url):
    assert await _read(url, "skill://grafana/grafana-lgtm/_index.md") == (
        "# grafana/grafana-lgtm — 3 skills\n\n"
        "skill://grafana/grafana-lgtm/loki/SKILL.md: Query logs.\n"
        "skill://grafana/grafana-lgtm/tempo/SKILL.md: Query traces.\n"
        "skill://grafana/grafana-lgtm/testing/SKILL.md: Test the stack.\n"
        "\nRead any URI above for that skill's instructions. Read _manifest in"
        " place of SKILL.md to see what else it ships, then read one of those"
        " paths under the same skill.\n"
    )
    assert "sdk" in await _read(url, "skill://grafana/grafana-plugins/app/_index.md")
    intermediate = "skill://grafana/grafana-plugins/_index.md"
    assert "No resource" in await _tool(url, intermediate)


async def test_the_files_index_lists_the_library_level_files(url):
    assert await _read(url, "skill://grafana/_files.md") == (
        "# grafana — 2 library-level files\n\n"
        "These sit outside every skill in this library, and its skills"
        " reference them.\n\n"
        "skill://grafana/shared/style.md\n"
        "skill://grafana/extra/notes.md\n"
    )


async def test_the_full_listing_adds_every_skills_instructions(url):
    uris = [row[0] for row in await _rows(url, "?skills=full")]
    skills = [u for u in uris if u.endswith("/SKILL.md")]
    assert skills == [
        "skill://grafana/grafana-k6/k6/SKILL.md",
        "skill://grafana/grafana-k6/testing/SKILL.md",
        "skill://grafana/grafana-lgtm/loki/SKILL.md",
        "skill://grafana/grafana-lgtm/tempo/SKILL.md",
        "skill://grafana/grafana-lgtm/testing/SKILL.md",
        "skill://grafana/grafana-ops/runbook/SKILL.md",
        "skill://grafana/grafana-plugins/app/sdk/SKILL.md",
        "skill://grafana/overview/SKILL.md",
        "skill://other/grafana-lgtm/loki/SKILL.md",
    ]
    assert uris[: -len(skills)] == [row[0] for row in await _rows(url)]


# -- scope --------------------------------------------------------------------------

LGTM = "grafana/grafana-lgtm"


async def test_a_folder_scope_lists_its_folder_and_its_sources_files(url):
    assert [row[0] for row in await _rows(url, library=LGTM)] == [
        "skill://grafana/_index.md",
        "skill://grafana/grafana-lgtm/_index.md",
        "skill://grafana/_files.md",
    ]
    files = await _read(url, "skill://grafana/_files.md", library=LGTM)
    assert "skill://grafana/shared/style.md" in files
    assert "extra/notes.md" not in files


async def test_a_folder_scope_cannot_read_another_sources_library_file(url):
    """`extra/notes.md` belongs to grafana-extra, which has nothing in grafana-lgtm."""
    assert await _read(url, "skill://grafana/shared/style.md", library=LGTM)
    error = await _read_error(url, "skill://grafana/extra/notes.md", library=LGTM)
    assert "not found" in error
    assert "ops notes" not in await _tool(
        url, "skill://grafana/extra/notes.md", library=LGTM
    )


async def test_a_folder_scope_sees_only_the_prompts_of_its_sources(url):
    async with _client(url, library=LGTM) as client:
        folder = sorted(p.name for p in await client.list_prompts())
    async with _client(url, library="grafana") as client:
        library = sorted(p.name for p in await client.list_prompts())
    assert folder == ["grafana_debug"]
    assert library == ["grafana_debug", "grafana_ops"]


async def test_a_library_scope_reaches_every_source_of_the_library(url):
    assert await _read(url, "skill://grafana/extra/notes.md", library="grafana")
    assert await _read(url, "skill://grafana/shared/style.md", library="grafana")


async def test_a_bare_folder_name_selects_nothing(url):
    assert await _rows(url, library="grafana-lgtm") == []


async def test_a_library_file_named_manifest_is_not_labelled_json(tmp_path):
    """Only a skill's `_manifest` is JSON; a library file of that name is not."""
    from fastmcp import Client

    from kubed.mcp_kb import KnowledgeBase
    from kubed.mcp_kb.config import Config

    root = tmp_path / "src"
    (root / "skills" / "a").mkdir(parents=True)
    (root / "skills" / "a" / "SKILL.md").write_text(
        "---\nname: a\ndescription: a\n---\nbody\n"
    )
    (root / "notes").mkdir()
    (root / "notes" / "_manifest").write_text("plain words\n")
    config = Config.model_validate(
        {
            "sources": [
                {
                    "name": "lib",
                    "url": f"file://{root}",
                    "include": {"files": ["notes/**/*"]},
                }
            ]
        }
    )
    kb = KnowledgeBase(config, tmp_path / "cache")

    async with Client(kb.mcp) as client:
        library_file = (await client.read_resource("skill://lib/notes/_manifest"))[0]
        manifest = (await client.read_resource("skill://lib/a/_manifest"))[0]

    assert library_file.text == "plain words\n"
    assert library_file.mime_type == "text/markdown"
    assert manifest.mime_type == "application/json"


async def test_a_skill_nested_inside_a_skill_is_served_as_the_spec_says(tmp_path):
    """SEP-2640 allows a skill inside another skill's directory.

    Both are published flat, each at its own URI; the enclosing skill's
    manifest lists the nested skill's files as its own supporting files; and a
    URI under both names one file, so reaching it through either skill reads the
    same bytes. Nothing about the nesting is a conflict, and nothing is skipped.
    """
    import json

    from fastmcp import Client

    from kubed.mcp_kb import KnowledgeBase
    from kubed.mcp_kb.config import Config

    root = tmp_path / "src"
    (root / "skills" / "x" / "y" / "refs").mkdir(parents=True)
    (root / "skills" / "x" / "SKILL.md").write_text(
        "---\nname: x\ndescription: outer\n---\nsee y/refs/a.md\n"
    )
    (root / "skills" / "x" / "y" / "SKILL.md").write_text(
        "---\nname: y\ndescription: inner\n---\ninner body\n"
    )
    (root / "skills" / "x" / "y" / "refs" / "a.md").write_text("shared bytes\n")
    config = Config.model_validate(
        {"sources": [{"name": "lib", "url": f"file://{root}"}]}
    )
    kb = KnowledgeBase(config, tmp_path / "cache")

    async with Client(kb.mcp) as client:
        index = (await client.read_resource("skill://lib/_index.md"))[0].text
        inner = (await client.read_resource("skill://lib/x/y/SKILL.md"))[0].text
        shared = (await client.read_resource("skill://lib/x/y/refs/a.md"))[0].text
        manifest = (await client.read_resource("skill://lib/x/_manifest"))[0].text

    assert "skill://lib/x/SKILL.md: outer" in index
    assert "skill://lib/x/y/SKILL.md: inner" in index
    assert "inner body" in inner
    assert shared == "shared bytes\n"
    assert {"y/SKILL.md", "y/refs/a.md"} <= {f["path"] for f in json.loads(manifest)["files"]}
    assert "skipped" not in kb.snapshot.status["lib"]
