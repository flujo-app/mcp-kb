"""The ``skill://`` grammar, read back through a running server.

The tree is shaped like the grafana library upstream: skills two levels down
under folders (``skills/<folder>/<skill>/SKILL.md``), a skill directly in the
skill root, a skill nested deeper still, two skills sharing a name in different
folders, a library-level file, and a prompt. A second plugin feeds the same
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


# The two placeholders this server is the authority on -- the plugin root and
# the skill's own directory -- in a skill's instructions and in a file beside
# them, which are two different answers.
PLACEHOLDERS = (
    "Style: ${CLAUDE_PLUGIN_ROOT}/shared/style.md."
    " Links: ${CLAUDE_SKILL_DIR}/references/LINKS.md."
    " The editor has ${selection}.\n"
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
    _skill(
        grafana,
        "skills/grafana-lgtm/tempo",
        "tempo",
        "Query traces.",
        PLACEHOLDERS,
    )
    _write(grafana, "skills/grafana-lgtm/tempo/references/LINKS.md", PLACEHOLDERS)
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
            "plugins": [
                {
                    "name": "grafana",
                    "source": f"file://{grafana}",
                    "tags": ["core"],
                    "files": ["shared/**"],
                },
                {
                    "name": "grafana-extra",
                    "source": f"file://{extra}",
                    "tags": ["extra"],
                    "files": ["extra/**"],
                },
                {"name": "other", "source": f"file://{other}"},
                {
                    "name": "handbook",
                    "source": f"file://{handbook}",
                    "skills": [],
                    "prompts": [],
                    "files": ["guide/**"],
                },
            ],
            "libraries": [
                {"name": "grafana", "plugins": ["grafana", "grafana-extra"]},
                {"name": "other", "plugins": ["other"]},
                {"name": "handbook", "plugins": ["handbook"]},
            ],
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


def _client(url, query="", library=None, tags=None):
    headers = {}
    if library:
        headers["X-Skill-Library"] = library
    if tags:
        headers["X-Skill-Tags"] = tags
    return Client(StreamableHttpTransport(url + query, headers=headers))


async def _read(url, uri, library=None, tags=None):
    async with _client(url, library=library, tags=tags) as client:
        return (await client.read_resource(uri))[0].text


async def _read_error(url, uri, library=None, tags=None):
    async with _client(url, library=library, tags=tags) as client:
        with pytest.raises(Exception) as caught:
            await client.read_resource(uri)
    return str(caught.value)


async def _raw(url, uri, library=None, tags=None):
    """``resources/read`` with ``uri`` sent exactly as given.

    ``Client.read_resource`` passes the URI through pydantic's ``AnyUrl``,
    which resolves ``.`` and ``..`` before it leaves the client -- so it can
    never show what a client that sends the raw string gets. The session
    underneath takes a plain ``str`` and sends it as written, over the same
    HTTP connection.
    """
    async with _client(url, library=library, tags=tags) as client:
        try:
            result = await client.session.read_resource(uri)
        except Exception as exc:  # noqa: BLE001 - the error text is the result
            return f"error: {exc}"
    return result.contents[0].text


async def _tool(url, uri, library=None, tags=None):
    """The mirror's answer, as text, whether it is a body or an error result."""
    async with _client(url, library=library, tags=tags) as client:
        result = await client.call_tool(
            "read_resource", {"uri": uri}, raise_on_error=False
        )
    return result.content[0].text


async def _rows(url, query="", library=None, tags=None):
    async with _client(url, query, library, tags) as client:
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


async def test_a_library_level_file_keeps_its_path_from_the_plugin_root(url):
    assert await _read(url, "skill://grafana/shared/style.md") == "house style\n"
    assert await _read(url, "skill://grafana/extra/notes.md") == "ops notes\n"


# -- the placeholders this server can answer --------------------------------------

TEMPO = "skill://grafana/grafana-lgtm/tempo"
RESOLVED = (
    "Style: skill://grafana/shared/style.md."
    f" Links: {TEMPO}/references/LINKS.md."
    " The editor has ${selection}.\n"
)


async def test_a_skills_instructions_name_the_plugin_root_and_its_own_directory(url):
    """Both become addresses, and both addresses read. Everything else that is
    the client's to fill -- ``${selection}`` here -- is served as written."""
    body = await _read(url, f"{TEMPO}/SKILL.md")
    assert body.endswith(RESOLVED)
    assert body == await _tool(url, f"{TEMPO}/SKILL.md")
    assert await _read(url, "skill://grafana/shared/style.md") == "house style\n"
    assert await _read(url, f"{TEMPO}/references/LINKS.md")


async def test_every_other_file_of_the_skill_is_served_verbatim(url):
    """``_manifest`` publishes a size and a hash of the bytes on disk, so only
    the instructions are rewritten."""
    assert await _read(url, f"{TEMPO}/references/LINKS.md") == PLACEHOLDERS
    manifest = json.loads(await _read(url, f"{TEMPO}/_manifest"))
    sizes = {f["path"]: f["size"] for f in manifest["files"]}
    assert sizes["references/LINKS.md"] == len(PLACEHOLDERS)


async def test_a_prompt_body_names_the_plugin_root_in_every_dialect(tmp_path):
    """A prompt is not a skill and has no directory of its own, so
    ``${CLAUDE_SKILL_DIR}`` stays as written; the plugin root is answered.

    Through both surfaces: the mirror renders the same prompt, so a client
    with no prompts of its own reads the same addresses."""
    root = tmp_path / "plugin"
    cite = "Read ${CLAUDE_PLUGIN_ROOT}/shared/x.md, not ${CLAUDE_SKILL_DIR}.\n"
    _write(root, "prompts/own.md", f"---\ndescription: Ours.\n---\n{cite}")
    _write(root, "commands/theirs.md", f"---\ndescription: Claude's.\n---\n{cite}")
    _write(
        root,
        ".github/prompts/vscode.prompt.md",
        f"---\ndescription: Copilot's.\n---\n{cite}",
    )
    config = Config.model_validate(
        {
            "plugins": [{"name": "kit", "source": f"file://{root}"}],
            "libraries": [{"name": "kit", "plugins": ["kit"]}],
        }
    )
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")
    expected = "Read skill://kit/shared/x.md, not ${CLAUDE_SKILL_DIR}.\n"

    async with Client(knowledge_base.mcp) as client:
        rendered = {
            name: (await client.get_prompt(name)).messages[0].content.text
            for name in ("kit_own", "kit_theirs", "kit_vscode")
        }
        mirrored = await client.call_tool("get_prompt", {"name": "kit_own"})
    assert {p.dialect for p in knowledge_base.prompts} == {
        "mcp-kb",
        "claude",
        "copilot",
    }
    assert set(rendered.values()) == {expected}
    assert json.loads(mirrored.content[0].text)["messages"] == [
        {"role": "user", "content": expected}
    ]


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
    # `extra` admits grafana-extra alone, which has no loki.
    hidden = await _raw(url, sneak, tags="extra")
    assert hidden == (await _raw(url, missing, tags="extra")).replace(missing, sneak)
    hidden = await _tool(url, sneak, tags="extra")
    assert hidden == (await _tool(url, missing, tags="extra")).replace(missing, sneak)


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
    ("skill://grafana/grafana-plugins", "skill://grafana/grafana-plugins/_index.md"),
    ("skill://grafana/shared", "skill://grafana/_files.md"),
    ("skill://handbook", "skill://handbook/_files.md"),
    (LOKI, f"{LOKI}/SKILL.md"),
]


@pytest.mark.parametrize(("uri", "instead"), DIRECTORIES)
async def test_a_directory_is_not_found_and_names_the_file_to_read(url, uri, instead):
    error = await _read_error(url, uri)
    assert "not found" in error
    assert instead in error
    async with _client(url) as client:
        mirrored = await client.call_tool(
            "read_resource", {"uri": uri}, raise_on_error=False
        )
    assert mirrored.is_error
    assert mirrored.content[0].text.startswith(f"No resource at '{uri}'.")
    assert instead in mirrored.content[0].text


CITED_FROM_A_SKILL = "skill://grafana/grafana-lgtm/loki/shared/style.md"


async def test_a_library_file_cited_from_inside_a_skill_reads_there(url):
    """Skills cite shared material from the repository root, which is their
    plugin root: the citation reads at the address it resolves to."""
    assert await _read(url, CITED_FROM_A_SKILL) == "house style\n"
    assert await _tool(url, CITED_FROM_A_SKILL) == "house style\n"
    # Another plugin's root is not this skill's, in either direction.
    for uri in (
        "skill://grafana/grafana-lgtm/loki/extra/notes.md",
        "skill://grafana/grafana-ops/runbook/shared/style.md",
    ):
        assert "not found" in await _read_error(url, uri)


async def test_a_skill_root_names_its_manifest_too(url):
    assert f"{LOKI}/_manifest" in await _read_error(url, LOKI)
    assert f"{LOKI}/_manifest" in await _tool(url, LOKI)


async def test_a_missing_address_names_no_file(url):
    error = await _read_error(url, "skill://grafana/nope")
    assert "not found" in error and "_index.md" not in error
    assert "list_resources" in await _tool(url, "skill://grafana/nope")


OUT_OF_SCOPE = [
    # (a directory unscoped, a missing address beside it, a scope that hides it)
    ("skill://grafana/grafana-lgtm", "skill://grafana/nope", {"tags": "extra"}),
    (
        "skill://grafana/grafana-lgtm/loki",
        "skill://grafana/grafana-lgtm/nope",
        {"tags": "extra"},
    ),
    ("skill://grafana/extra", "skill://grafana/nope", {"tags": "core"}),
    ("skill://grafana", "skill://nope", {"library": "other"}),
    ("skill://handbook", "skill://nope", {"library": "other"}),
]


@pytest.mark.parametrize(("uri", "missing", "scope"), OUT_OF_SCOPE)
async def test_an_out_of_scope_directory_reads_as_a_missing_one(
    url, uri, missing, scope
):
    """The hint must not confirm a directory the caller was not given.

    Unpinned, each address gets a hint a missing one does not, so the case is
    live; pinned, the whole message matches the missing address's, through the
    resource and through the tool.
    """
    unpinned = await _read_error(url, uri)
    assert unpinned != (await _read_error(url, missing)).replace(missing, uri)

    hidden = await _read_error(url, uri, **scope)
    absent = await _read_error(url, missing, **scope)
    assert hidden == absent.replace(missing, uri)

    hidden = await _tool(url, uri, **scope)
    absent = await _tool(url, missing, **scope)
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
            " grafana-k6, grafana-lgtm, grafana-ops, grafana-plugins.",
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
            "skill://grafana/grafana-plugins/_index.md",
            "grafana/grafana-plugins/_index.md",
            "1 skill in the grafana-plugins folder of the grafana library.",
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


SKILL_ADVICE = (
    "Read a skill's SKILL.md for its instructions, or _manifest in its place to"
    " see what else it ships, then read one of those paths under the same skill."
)


async def test_the_library_index_lists_its_folders_skills_and_files(url):
    """One line per folder, not per skill: the reader picks where to go deeper."""
    assert await _read(url, "skill://grafana/_index.md") == (
        "# grafana — 8 skills\n\n"
        "skill://grafana/grafana-k6/_index.md: 2 skills\n"
        "skill://grafana/grafana-lgtm/_index.md: 3 skills\n"
        "skill://grafana/grafana-ops/_index.md: 1 skill\n"
        "skill://grafana/grafana-plugins/_index.md: 1 skill\n"
        "skill://grafana/overview/SKILL.md: Start here.\n"
        "skill://grafana/_files.md: 2 library-level files\n"
        "\nRead a folder's _index.md for what is in it. " + SKILL_ADVICE + "\n"
    )


async def test_a_folder_index_lists_the_skills_directly_in_it(url):
    assert await _read(url, "skill://grafana/grafana-lgtm/_index.md") == (
        "# grafana/grafana-lgtm — 3 skills\n\n"
        "skill://grafana/grafana-lgtm/loki/SKILL.md: Query logs.\n"
        "skill://grafana/grafana-lgtm/tempo/SKILL.md: Query traces.\n"
        "skill://grafana/grafana-lgtm/testing/SKILL.md: Test the stack.\n"
        "\n" + SKILL_ADVICE + "\n"
    )


async def test_a_folder_holding_only_folders_has_an_index_of_them(url):
    assert await _read(url, "skill://grafana/grafana-plugins/_index.md") == (
        "# grafana/grafana-plugins — 1 skill\n\n"
        "skill://grafana/grafana-plugins/app/_index.md: 1 skill\n"
        "\nRead a folder's _index.md for what is in it.\n"
    )
    assert "sdk/SKILL.md" in await _read(
        url, "skill://grafana/grafana-plugins/app/_index.md"
    )


async def test_every_uri_an_index_names_reads(url):
    """Walked from the library indexes down, through both surfaces."""
    seen, queue = set(), [row[0] for row in await _rows(url)]
    while queue:
        uri = queue.pop()
        if uri in seen:
            continue
        seen.add(uri)
        body = await _read(url, uri)
        assert body
        if uri.endswith(("/_index.md", "/_files.md")):
            queue += re.findall(r"^(skill://\S+?):? ", body + " ", re.M)
            queue += re.findall(r"^(skill://\S+)$", body, re.M)
    assert "skill://grafana/grafana-plugins/app/sdk/SKILL.md" in seen
    assert "skill://grafana/shared/style.md" in seen


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


async def test_a_tag_scope_lists_the_library_with_the_plugins_carrying_it(url):
    """The listing is always the library level and its top folders: a tag
    narrows which plugins count, so the folders and files are theirs alone."""
    assert [row[0] for row in await _rows(url, tags="core")] == [
        "skill://grafana/_index.md",
        "skill://grafana/grafana-k6/_index.md",
        "skill://grafana/grafana-lgtm/_index.md",
        "skill://grafana/grafana-plugins/_index.md",
        "skill://grafana/_files.md",
    ]
    files = await _read(url, "skill://grafana/_files.md", tags="core")
    assert "skill://grafana/shared/style.md" in files
    assert "extra/notes.md" not in files


async def test_every_listed_folder_is_one_an_index_names(url):
    """Unscoped, the listing's folders are the library index's folders exactly."""
    listed = {r[0] for r in await _rows(url)} - {"skill://grafana/_index.md"}
    index = await _read(url, "skill://grafana/_index.md")
    named = set(re.findall(r"^(skill://\S+/_index\.md):", index, re.M))
    assert {u for u in listed if u.startswith("skill://grafana/")} - {
        "skill://grafana/_files.md"
    } == named


async def test_a_tag_scope_cannot_read_another_plugins_library_file(url):
    """`extra/notes.md` belongs to grafana-extra, which does not carry `core`."""
    assert await _read(url, "skill://grafana/shared/style.md", tags="core")
    error = await _read_error(url, "skill://grafana/extra/notes.md", tags="core")
    assert "not found" in error
    assert "ops notes" not in await _tool(
        url, "skill://grafana/extra/notes.md", tags="core"
    )


async def test_a_tag_scope_sees_only_the_prompts_of_the_plugins_carrying_it(url):
    async with _client(url, tags="core") as client:
        tagged = sorted(p.name for p in await client.list_prompts())
    async with _client(url, library="grafana") as client:
        library = sorted(p.name for p in await client.list_prompts())
    assert tagged == ["grafana_debug"]
    assert library == ["grafana_debug", "grafana_ops"]


async def test_a_library_scope_reaches_every_plugin_of_the_library(url):
    assert await _read(url, "skill://grafana/extra/notes.md", library="grafana")
    assert await _read(url, "skill://grafana/shared/style.md", library="grafana")


async def test_a_library_slash_folder_is_refused_as_not_a_library(url):
    """A scope names a library and nothing below it; the refusal says how to
    narrow instead."""
    with pytest.raises(Exception) as caught:
        await _rows(url, library="grafana/grafana-plugins")
    assert "use ?categories= or ?tags= to narrow inside it" in str(caught.value)


async def test_a_bare_folder_name_is_refused_as_no_such_library(url):
    """A folder name is not a library, and is not looked for in every library."""
    with pytest.raises(Exception, match=r"no such library\. The libraries are:"):
        await _rows(url, library="grafana-lgtm")


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
            "plugins": [
                {"name": "lib", "source": f"file://{root}", "files": ["notes/**/*"]}
            ],
            "libraries": [{"name": "lib", "plugins": ["lib"]}],
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
        {
            "plugins": [{"name": "lib", "source": f"file://{root}"}],
            "libraries": [{"name": "lib", "plugins": ["lib"]}],
        }
    )
    kb = KnowledgeBase(config, tmp_path / "cache")

    async with Client(kb.mcp) as client:
        index = (await client.read_resource("skill://lib/_index.md"))[0].text
        folder = (await client.read_resource("skill://lib/x/_index.md"))[0].text
        inner = (await client.read_resource("skill://lib/x/y/SKILL.md"))[0].text
        shared = (await client.read_resource("skill://lib/x/y/refs/a.md"))[0].text
        manifest = (await client.read_resource("skill://lib/x/_manifest"))[0].text

    assert "skill://lib/x/SKILL.md: outer" in index
    assert "skill://lib/x/_index.md: 1 skill" in index
    assert "skill://lib/x/y/SKILL.md: inner" in folder
    assert "inner body" in inner
    assert shared == "shared bytes\n"
    assert {"y/SKILL.md", "y/refs/a.md"} <= {f["path"] for f in json.loads(manifest)["files"]}
    assert "skipped" not in kb.snapshot.status["plugins"]["lib"]
