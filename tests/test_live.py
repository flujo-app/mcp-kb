"""``cache: live``: a read revalidates the file it is about to serve.

Every test drives a real ``School`` over the real WebDAV server
(``tests/webdav_server.py``), which refuses an unauthenticated request. What is
being proved is one sentence: *a file edited upstream is served on the next
read, and everything else about the server stays where it was.*

Two quirks of the server under test:

- Its ETag is ``inode-mtime-size``, so a test that edits a file makes it a
  different length. Nextcloud's ETags are content-derived and have no such
  requirement.
- The revalidation TTL is off by default here (``TTL_SECONDS`` monkeypatched to
  zero), because a test that edits a file and reads it again does so in
  microseconds. The one test about the TTL turns it back on.
"""

import httpx
import pytest

from mcp_school import School, live
from mcp_school.config import Config
from mcp_school.prompts import PromptProvider
from tests.webdav_server import PASSWORD, USERNAME

pytestmark = pytest.mark.unit

ENV = "WEBDAV_PASSWORD"
URI = "skill://notes/x"
GUIDE = "skill://notes/docs/guide.md"
PROMPT = "---\ndescription: A prompt.\n---\n\n{body}\n"


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    """The password the config only ever names."""
    monkeypatch.setenv(ENV, PASSWORD)


@pytest.fixture(autouse=True)
def no_ttl(monkeypatch):
    """Ask the server on every read, because a test edits faster than 2 seconds."""
    monkeypatch.setattr(live, "TTL_SECONDS", 0.0)


def _school(webdav, tmp_path, cache="live"):
    config = Config.model_validate(
        {
            "sources": [
                {
                    "name": "notes",
                    "url": webdav.url,
                    "auth": {"username": USERNAME, "password": {"env": ENV}},
                    "cache": cache,
                    "include": {
                        "skills": ["skills/*/SKILL.md"],
                        "prompts": ["prompts/*.md"],
                        "files": ["docs/**/*"],
                    },
                }
            ]
        }
    )
    return School(config, tmp_path / "cache")


# -- what live mode buys -------------------------------------------------------


def test_an_edit_upstream_is_visible_on_the_next_read_without_a_refresh(
    webdav, tmp_path
):
    school = _school(webdav, tmp_path)
    assert "first" in school.catalogue.read(URI)

    webdav.skill("x", "edited in nextcloud, and rather longer than before")

    assert "edited in nextcloud" in school.catalogue.read(URI)
    assert school.generation == 0, "no refresh happened; the read did the work"


def test_a_pack_level_file_is_revalidated_too(webdav, tmp_path):
    """A skill's instructions are not the only thing a live source serves."""
    school = _school(webdav, tmp_path)
    assert school.catalogue.read(GUIDE) == "pack-level guidance\n"

    webdav.write("docs/guide.md", "guidance, revised and lengthened upstream\n")

    assert "revised and lengthened" in school.catalogue.read(GUIDE)


async def test_a_live_prompt_renders_the_edited_body(webdav, tmp_path):
    """A prompt's body lives in memory, so live mode has to re-read it."""
    webdav.write("prompts/p.md", PROMPT.format(body="the first body"))
    school = _school(webdav, tmp_path)
    provider = PromptProvider(lambda: school.snapshot)

    prompt = await provider.get_prompt("notes_p")
    assert "the first body" in await prompt.render({})

    webdav.write("prompts/p.md", PROMPT.format(body="the second body, much longer"))

    prompt = await provider.get_prompt("notes_p")
    assert "the second body" in await prompt.render({})


# -- and what it does not ------------------------------------------------------


def test_a_new_upstream_file_needs_a_refresh(webdav, tmp_path):
    """Revalidation prices a file that has a URI. A new one has none yet."""
    school = _school(webdav, tmp_path)
    webdav.skill("y", "a skill added after the folder was indexed")

    assert school.catalogue.read("skill://notes/y") is None
    assert [s.name for s in school.index.visible()] == ["x"]

    assert school.refresh() == ["notes"]
    assert "a skill added after" in school.catalogue.read("skill://notes/y")


def test_reads_within_the_ttl_do_not_hit_the_server(webdav, tmp_path, monkeypatch):
    """One read of a skill is several reads of its files; one PROPFIND is enough."""
    monkeypatch.setattr(live, "TTL_SECONDS", 60.0)
    school = _school(webdav, tmp_path)
    school.catalogue.read(URI)
    webdav.requests.clear()

    school.catalogue.read(URI)
    school.catalogue.read(URI)

    assert webdav.requests == []


def test_a_snapshot_source_never_revalidates(webdav, tmp_path, monkeypatch):
    """The default dial is disk and nothing else: not one request on a read."""
    school = _school(webdav, tmp_path, cache="snapshot")
    reached = []

    def boom(*args, **kwargs):
        # Recorded as well as raised: a revalidator swallows what a read
        # raises, so a raise alone would leave this test green either way.
        reached.append(args)
        raise AssertionError("a snapshot source must not touch the network")

    monkeypatch.setattr(live, "fetch_file", boom)
    webdav.requests.clear()

    assert "first" in school.catalogue.read(URI)
    assert school.catalogue.read(GUIDE) == "pack-level guidance\n"
    assert reached == [], "the read path never reached the network"
    assert webdav.requests == []


# -- degrading --------------------------------------------------------------


def test_a_flaky_server_degrades_to_the_cached_copy(webdav, tmp_path):
    """A read is answered from disk whatever the network is doing.

    The copy is on disk and complete; a server that has stopped answering is a
    reason to serve it unrevalidated, never a reason to fail the read.
    """
    school = _school(webdav, tmp_path)
    assert "first" in school.catalogue.read(URI)

    webdav.stop()

    assert "first" in school.catalogue.read(URI)
    assert school.catalogue.read(GUIDE) == "pack-level guidance\n"


def test_a_failed_revalidation_says_nothing_about_the_credentials(
    webdav, tmp_path, caplog
):
    school = _school(webdav, tmp_path)
    school.catalogue.read(URI)
    webdav.stop()

    with caplog.at_level("WARNING"):
        school.catalogue.read(URI)

    assert caplog.text, "a revalidation that failed is worth a line in the log"
    assert PASSWORD not in caplog.text
    assert USERNAME not in caplog.text


# -- the machinery ------------------------------------------------------------


def test_the_revalidator_follows_the_export_a_refresh_created(webdav, tmp_path):
    """A refresh moves the source to a new directory; live reads must follow it.

    A revalidator that outlived its snapshot would go on writing into the
    export nothing is serving any more, and the edit would never appear.
    """
    school = _school(webdav, tmp_path)
    retired = school.index.get("x").path
    webdav.skill("y", "a second skill, which moves the whole folder's digest")
    assert school.refresh() == ["notes"]
    current = school.index.get("x").path
    assert current != retired

    webdav.skill("x", "edited after the refresh, and longer than it was")

    assert "edited after the refresh" in school.catalogue.read(URI)
    assert "first" in (retired / "SKILL.md").read_text(), "the retired export stands"


def test_one_client_serves_every_live_read(webdav, tmp_path, monkeypatch):
    """A client per read is a TCP connection and a TLS handshake per read."""
    school = _school(webdav, tmp_path)
    built = []
    original = live.client

    def counted(source):
        built.append(source.name)
        return original(source)

    monkeypatch.setattr(live, "client", counted)

    school.catalogue.read(URI)
    school.catalogue.read(URI)
    school.catalogue.read(GUIDE)

    assert built == ["notes"]


async def test_health_reports_what_a_live_source_revalidated(webdav, tmp_path):
    school = _school(webdav, tmp_path)
    school.catalogue.read(URI)
    webdav.skill("x", "edited in nextcloud, and rather longer than before")
    school.catalogue.read(URI)

    transport = httpx.ASGITransport(app=school.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://school") as http:
        body = (await http.get("/health")).json()

    source = body["sources"]["notes"]
    assert source["live"] is True
    assert source["revalidated"] == 2
    assert source["fetched"] == 1


async def test_health_says_nothing_about_the_backend_or_the_credentials(
    webdav, tmp_path
):
    """A backend is config-only, and /health is not where it stops being one."""
    school = _school(webdav, tmp_path)
    school.catalogue.read(URI)

    transport = httpx.ASGITransport(app=school.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://school") as http:
        body = (await http.get("/health")).text

    assert PASSWORD not in body
    assert "127.0.0.1" not in body
    assert "SKILL.md" not in body, "the fingerprint is a digest, not a file listing"


def test_a_snapshot_source_reports_no_live_fields(webdav, tmp_path):
    school = _school(webdav, tmp_path, cache="snapshot")

    assert "live" not in school.status["notes"]
