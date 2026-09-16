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

import socket
import threading
import time

import httpx
import pytest

from mcp_school import School, live
from mcp_school.config import Config, WebdavSource
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


def test_a_failed_revalidation_names_the_source_once(webdav, tmp_path, caplog):
    """fetch_file's error already carries the source, and the log line adds it:
    `notes: revalidating … failed: notes: …` reads as two sources."""
    school = _school(webdav, tmp_path)
    school.catalogue.read(URI)
    webdav.stop()

    with caplog.at_level("WARNING"):
        school.catalogue.read(URI)

    assert caplog.text.count("notes:") == 1
    assert caplog.text.count("skills/x/SKILL.md") == 1


def test_a_server_that_hangs_is_asked_once_and_then_left_alone(
    tmp_path, black_hole, monkeypatch, caplog
):
    """The measured collapse: every read paid httpx's 5s default against a 2s
    TTL, and repeats of the *same* file paid it too.

    Two things stop it. The revalidation client has a timeout well under the
    TTL, so one read costs a bounded moment; and a failure puts the source in a
    cooldown, so nothing after the first pays anything at all.
    """
    monkeypatch.setattr(live, "REVALIDATE_TIMEOUT", 0.2)
    monkeypatch.setattr(live, "COOLDOWN_SECONDS", 60.0)
    school = black_hole(tmp_path)

    with caplog.at_level("WARNING"):
        first = _elapsed(school, URI)
        after = [_elapsed(school, URI) for _ in range(5)]
        other = _elapsed(school, GUIDE)

    assert "first" in school.catalogue.read(URI), "the cached copy is still served"
    assert first < 1.0, f"one read must not cost the httpx default: {first:.2f}s"
    assert max(after) < 0.05, f"a wedged server must cost nothing twice: {after}"
    assert other < 0.05, "the cooldown covers the source, not the one file"
    assert caplog.text.count("revalidating") == 1


async def test_health_says_a_live_source_is_cooling_after_a_failure(
    webdav, tmp_path, monkeypatch
):
    """An operator reading /health has to be able to tell "nothing changed"
    from "we stopped asking"."""
    school = _school(webdav, tmp_path)
    school.catalogue.read(URI)
    monkeypatch.setattr(live, "fetch_file", _wedged)
    school.catalogue.read(URI)

    transport = httpx.ASGITransport(app=school.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://school") as http:
        body = (await http.get("/health")).json()

    assert body["sources"]["notes"]["cooling"] is True


def test_a_source_that_answers_again_stops_cooling(webdav, tmp_path, monkeypatch):
    """The cooldown is a pause, not a verdict: a folder comes back by itself."""
    monkeypatch.setattr(live, "COOLDOWN_SECONDS", 0.05)
    school = _school(webdav, tmp_path)
    answering = live.fetch_file
    monkeypatch.setattr(live, "fetch_file", _wedged)
    school.catalogue.read(URI)
    assert school.snapshot.stats("notes")["cooling"] is True

    monkeypatch.setattr(live, "fetch_file", answering)
    time.sleep(0.1)
    school.catalogue.read(URI)

    assert "cooling" not in school.snapshot.stats("notes")


def test_the_ttl_runs_from_the_answer_and_not_from_the_question(
    webdav, tmp_path, monkeypatch
):
    """Stamped before the request but measured against elapsed time, a request
    slower than the TTL leaves the same file due for another the instant it
    comes back -- so a slow server is asked once per read, not once per TTL."""
    monkeypatch.setattr(live, "TTL_SECONDS", 0.3)
    school = _school(webdav, tmp_path)
    school.catalogue.read(URI)
    answering = live.fetch_file

    def slow(*args, **kwargs):
        time.sleep(0.4)
        return answering(*args, **kwargs)

    time.sleep(0.35)
    monkeypatch.setattr(live, "fetch_file", slow)
    school.catalogue.read(URI)

    monkeypatch.setattr(live, "fetch_file", answering)
    webdav.requests.clear()
    school.catalogue.read(URI)

    assert webdav.requests == [], "the TTL must bound a slow server too"


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

    def counted(source, **kwargs):
        built.append(source.name)
        return original(source, **kwargs)

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


# -- helpers -------------------------------------------------------------------


def _wedged(*args, **kwargs):
    """A revalidation against a server that never answers."""
    raise TimeoutError("timed out")


def _elapsed(school, uri: str) -> float:
    start = time.monotonic()
    school.catalogue.read(uri)
    return time.monotonic() - start


@pytest.fixture
def black_hole(webdav, monkeypatch):
    """A school whose revalidations go to a socket that accepts and never answers.

    Not "refused", which is instant and is what the flaky-server test already
    covers. This is the Nextcloud failure mode that costs a timeout. The folder
    is indexed against the real server, so the copy on disk is whole and only
    the revalidation is wedged -- which is exactly the shape of the failure:
    every byte is already on disk and the server is what has gone quiet.
    """

    def build(tmp_path):
        school = _school(webdav, tmp_path)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        accepted: list[socket.socket] = []

        def accept():
            while True:
                try:
                    accepted.append(listener.accept()[0])
                except OSError:
                    return

        threading.Thread(target=accept, daemon=True).start()
        void = WebdavSource(
            name="notes",
            url=f"webdav+http://127.0.0.1:{listener.getsockname()[1]}",
            auth={"username": USERNAME, "password": {"env": ENV}},
        )
        answering = live.client
        monkeypatch.setattr(live, "client", lambda _, **kwargs: answering(void, **kwargs))
        monkeypatch.setattr(live, "TTL_SECONDS", 0.0)
        return school

    return build
