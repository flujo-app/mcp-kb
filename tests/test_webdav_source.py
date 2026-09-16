"""``webdav+…`` sources: copy the folder, key it by its ETags, revalidate a file.

Every test runs against a real WebDAV server on a loopback port that refuses an
unauthenticated request (``tests/webdav_server.py``), so the credentials are
under test rather than assumed. No test here needs the network.

The server's ETag is ``inode-mtime-size``, which is why a test that edits a file
changes its length: two bodies of the same size written in the same second are
one ETag to wsgidav. Nextcloud's are content-derived and have no such quirk.
"""

import json
from http.client import HTTPConnection
from pathlib import Path

import pytest

from mcp_school import School
from mcp_school.config import Config, WebdavSource
from mcp_school.sources import SourceError, fingerprint, materialise
from mcp_school.sources import export as exports
from mcp_school.sources.webdav import ETAGS_FILE, VERSION_FILE, fetch_file
from tests.webdav_server import PASSWORD, USERNAME

pytestmark = pytest.mark.unit

ENV = "WEBDAV_PASSWORD"
SKILL = "skills/x/SKILL.md"


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    """The password the config only ever names, present for every test but one."""
    monkeypatch.setenv(ENV, PASSWORD)


def _source(webdav, **kwargs):
    return WebdavSource(
        name="notes",
        url=webdav.url,
        auth={"username": USERNAME, "password": {"env": ENV}},
        include={"skills": ["skills/*/SKILL.md"], "files": ["**/*"]},
        **kwargs,
    )


def _body(root):
    return (root / SKILL).read_text()


# -- the copy ------------------------------------------------------------------


def test_a_webdav_folder_is_copied_into_the_cache(webdav, tmp_path):
    root = materialise(_source(webdav), tmp_path)

    assert root.parent == tmp_path / "src" / "notes"
    assert "first" in _body(root)
    assert (root / "docs" / "guide.md").read_text() == "pack-level guidance\n"
    assert json.loads((root / ETAGS_FILE).read_text())[SKILL]
    assert (root / VERSION_FILE).read_text().split()[0] == root.name


def test_an_unchanged_folder_is_listed_again_but_never_downloaded_again(
    webdav, tmp_path
):
    """The ETag set is the version, so the listing alone answers "has it moved".

    A refresh of a folder that has not changed costs the PROPFINDs and not one
    byte of content -- and returns the export already being served, rather than
    writing over it.
    """
    source = _source(webdav)
    first = materialise(source, tmp_path)
    webdav.requests.clear()

    again = materialise(source, tmp_path)

    assert again == first
    assert webdav.method("GET") == []
    assert webdav.method("PROPFIND")


def test_an_edited_folder_is_exported_beside_the_one_being_served(webdav, tmp_path):
    """E3's rule: a refresh adds a tree, it never rewrites the one in use."""
    source = _source(webdav)
    old = materialise(source, tmp_path)
    webdav.skill("x", "second version")

    new = materialise(source, tmp_path)

    assert new != old
    assert "first" in _body(old)
    assert "second version" in _body(new)


def test_a_truncated_export_is_rebuilt(webdav, tmp_path):
    """A stamp is not proof: the tree must still hold the files it wrote."""
    source = _source(webdav)
    root = materialise(source, tmp_path)
    (root / SKILL).unlink()

    again = materialise(source, tmp_path)

    assert again == root
    assert "first" in _body(again)


def test_a_crashed_export_leaves_nothing_at_a_version_path(webdav, tmp_path):
    """What a crash may leave is a work directory, swept on the next pass."""
    source = _source(webdav)
    home = tmp_path / "src" / "notes"
    half = home / (exports.WORK_PREFIX + "0" * 64)
    half.mkdir(parents=True)
    (half / "leftover.md").write_text("never finished\n")

    root = materialise(source, tmp_path)

    assert not half.exists()
    assert not (root / "leftover.md").exists()


# -- authentication ------------------------------------------------------------


def test_the_server_under_test_refuses_an_anonymous_request(webdav):
    """The premise of every test above it.

    E3's git remote asked for nothing, so its whole suite passed while the one
    thing production needed -- authenticating -- had never run. A server that
    serves an anonymous reader would do the same here, quietly.
    """
    connection = HTTPConnection(webdav.url.removeprefix("webdav+http://"))
    connection.request("PROPFIND", "/", headers={"Depth": "1"})

    assert connection.getresponse().status == 401


def test_the_wrong_credentials_are_a_source_error_naming_the_status_not_the_password(
    webdav, tmp_path, monkeypatch
):
    """The failure an operator reads in /health says 401 and says nothing else."""
    monkeypatch.setenv(ENV, "not-the-password")

    with pytest.raises(SourceError) as raised:
        materialise(_source(webdav), tmp_path)

    message = str(raised.value)
    assert "401" in message
    assert message.startswith("notes:")
    assert PASSWORD not in message
    assert "not-the-password" not in message
    assert not (tmp_path / "src" / "notes").exists(), "a refused source exports nothing"


def test_a_missing_env_variable_fails_before_the_first_request(
    webdav, tmp_path, monkeypatch
):
    """A credential that was never configured is a config fault, and it is one
    whatever the server would have said: the error names the variable, and no
    request is made for it to answer with a 401."""
    monkeypatch.delenv(ENV, raising=False)
    webdav.requests.clear()

    with pytest.raises(SourceError) as raised:
        materialise(_source(webdav), tmp_path)

    assert ENV in str(raised.value)
    assert "401" not in str(raised.value)
    assert webdav.requests == []


def test_the_password_never_lands_under_the_cache(webdav, tmp_path):
    materialise(_source(webdav), tmp_path)

    written = [p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()]
    assert written
    assert not any(PASSWORD.encode() in blob for blob in written)


# -- the fingerprint -----------------------------------------------------------


def test_the_fingerprint_is_every_files_etag(webdav, tmp_path):
    source = _source(webdav)
    root = materialise(source, tmp_path)

    before = fingerprint(source, tmp_path, root)
    assert sorted(before["etags"]) == ["docs/guide.md", SKILL]
    assert fingerprint(source, tmp_path, root) == before, "an idle folder must compare equal"

    webdav.skill("x", "an edit that is longer than the first")
    edited = fingerprint(source, tmp_path, root)
    assert edited != before

    webdav.skill("y", "another skill")
    assert fingerprint(source, tmp_path, root) != edited


def test_the_fingerprint_moves_when_the_export_loses_a_file(webdav, tmp_path):
    """The remote has not changed, but what is being served has -- and a
    fingerprint that ignored that would report `ok` over a gutted tree."""
    source = _source(webdav)
    root = materialise(source, tmp_path)
    before = fingerprint(source, tmp_path, root)

    (root / SKILL).unlink()

    assert fingerprint(source, tmp_path, root) != before


# -- a live read's half: one file, revalidated ---------------------------------


def test_fetch_file_returns_the_etag_unchanged_when_the_file_has_not_moved(
    webdav, tmp_path
):
    source = _source(webdav)
    root = materialise(source, tmp_path)
    recorded = json.loads((root / ETAGS_FILE).read_text())[SKILL]
    webdav.requests.clear()

    assert fetch_file(source, root, SKILL) == recorded
    assert webdav.method("GET") == [], "an unchanged file is not downloaded"


def test_fetch_file_replaces_the_local_copy_when_the_etag_moved(webdav, tmp_path):
    source = _source(webdav)
    root = materialise(source, tmp_path)
    recorded = json.loads((root / ETAGS_FILE).read_text())[SKILL]
    webdav.skill("x", "edited in nextcloud, longer than before")

    moved = fetch_file(source, root, SKILL, etag=recorded)

    assert moved != recorded
    assert "edited in nextcloud" in _body(root)
    assert fetch_file(source, root, SKILL, etag=moved) == moved


def test_a_missing_upstream_file_leaves_the_local_copy(webdav, tmp_path):
    source = _source(webdav)
    root = materialise(source, tmp_path)
    (webdav.root / SKILL).unlink()

    assert fetch_file(source, root, SKILL) is None
    assert "first" in _body(root), "a deleted upstream file is not a deleted skill"


def test_fetch_file_refuses_a_path_that_leaves_the_export(webdav, tmp_path):
    source = _source(webdav)
    root = materialise(source, tmp_path)

    with pytest.raises(SourceError, match="outside"):
        fetch_file(source, root, "../escaped.md")


# -- through the catalogue -----------------------------------------------------


def test_a_webdav_source_is_served_without_naming_the_backend_anywhere(
    webdav, tmp_path
):
    """A backend is config-only: no cache path, no WebDAV URL, no scheme name."""
    config = Config.model_validate(
        {
            "sources": [
                {
                    "name": "notes",
                    "url": webdav.url,
                    "auth": {"username": USERNAME, "password": {"env": ENV}},
                    "include": {"skills": ["skills/*/SKILL.md"], "files": ["**/*"]},
                }
            ]
        }
    )
    school = School(config, tmp_path / "cache")

    assert [s.name for s in school.index.visible()] == ["x"]
    assert school.status["notes"]["status"] == "ok"
    assert "first" in school.catalogue.read("skill://notes/x")
    rows = "\n".join(str(entry) for entry in school.catalogue.entries())
    assert "webdav" not in rows
    assert "127.0.0.1" not in rows
    assert "cache" not in rows
    assert ETAGS_FILE not in rows and VERSION_FILE not in rows
    assert school.resources.files("notes") == ["docs/guide.md"]


def test_a_webdav_source_that_cannot_be_reached_fails_only_itself(webdav, tmp_path):
    """§C1.12: one bad source is a record, and the others are still served."""
    config = Config.model_validate(
        {
            "sources": [
                {
                    "name": "notes",
                    # A port nothing listens on: the failure is a connection
                    # refused rather than a status, and it must read as one.
                    "url": "webdav+http://127.0.0.1:1",
                    "auth": {"username": USERNAME, "password": {"env": ENV}},
                },
                {
                    "name": "local",
                    "url": f"file://{_local(tmp_path)}",
                    "include": {"skills": ["skills/*/SKILL.md"]},
                },
            ]
        }
    )
    school = School(config, tmp_path / "cache")

    assert school.status["notes"]["status"] == "failed"
    assert school.status["local"]["status"] == "ok"
    assert [s.name for s in school.index.visible()] == ["y"]


def _local(tmp_path: Path) -> Path:
    root = tmp_path / "local"
    skill = root / "skills" / "y"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: y\ndescription: Local.\n---\n\nbody\n")
    return root
