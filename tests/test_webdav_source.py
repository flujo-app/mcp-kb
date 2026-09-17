"""``webdav+…`` sources: copy the folder, key it by its ETags, revalidate a file.

Every test runs against a real WebDAV server on a loopback port that refuses an
unauthenticated request (``tests/webdav_server.py``), so the credentials are
under test rather than assumed. No test here needs the network.

The server's ETag is ``inode-mtime-size``, which is why a test that edits a file
changes its length: two bodies of the same size written in the same second are
one ETag to wsgidav. Nextcloud's are content-derived and have no such quirk.
"""

import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.config import Config, WebdavSource
from kubed.mcp_kb.sources import AccessRefused, SourceError, fingerprint, materialise
from kubed.mcp_kb.sources import export as exports
from kubed.mcp_kb.sources.webdav import ETAGS_FILE, VERSION_FILE, client, fetch_file
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
    assert (root / "docs" / "guide.md").read_text() == "library-level guidance\n"
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


def test_a_truncated_export_is_rebuilt_beside_the_one_being_served(webdav, tmp_path):
    """A stamp is not proof: the tree must still hold the files it wrote.

    And the repair goes to a directory of its own. The folder has not moved, so
    the version has not moved either, so the truncated tree is sitting at the
    exact path the live snapshot is serving out of -- rebuilding over it means
    deleting it under a reader.
    """
    source = _source(webdav)
    root = materialise(source, tmp_path)
    (root / SKILL).unlink()

    again = materialise(source, tmp_path)

    assert again != root, "the repair must not be published over the served tree"
    assert again.name.startswith(root.name), "still the same version, a later name"
    assert "first" in _body(again)
    assert (again / VERSION_FILE).read_text().split()[0] == root.name


def test_a_rebuild_never_interrupts_a_reader_of_the_served_tree(webdav, tmp_path):
    """Measured on this branch as 549 failed reads in 25 852; now none.

    The trigger is the one a live source makes real: a temp file left inside
    the export by a download that was killed makes the export's count disagree
    with its stamp, and the next pass rebuilds a version that has not moved.
    """
    source = _source(webdav)
    root = materialise(source, tmp_path)
    served = root / SKILL
    errors: list[str] = []
    stop = threading.Event()

    def read():
        while not stop.is_set():
            try:
                if "first" not in served.read_text():
                    errors.append("torn")
            except OSError as exc:
                errors.append(type(exc).__name__)

    readers = [threading.Thread(target=read, daemon=True) for _ in range(6)]
    for reader in readers:
        reader.start()
    try:
        current = root
        for round_ in range(4):
            (current / "skills" / "x" / f".tmp-{round_}").write_text("killed")
            current = materialise(source, tmp_path)
            time.sleep(0.05)
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=5)

    assert errors == []
    assert "first" in _body(root), "the tree the readers held is still whole"


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
    assert isinstance(raised.value, AccessRefused)
    assert "HTTP 401: the credentials were refused" in message
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


def test_a_missing_username_variable_fails_only_its_own_source(
    webdav, tmp_path, monkeypatch
):
    """The username may be an `{env:}` reference too, so it has the same way of
    being unset -- and resolving it outside the guard would make that a plain
    `ConfigError`, which `materialise_all` does not catch. One unset variable
    would then abort the whole pass instead of failing the source that declared
    it, which is the invariant every other failure here respects.
    """
    monkeypatch.setenv(ENV, PASSWORD)
    monkeypatch.delenv("WEBDAV_USER", raising=False)
    source = WebdavSource(
        name="notes",
        url=webdav.url,
        auth={"username": {"env": "WEBDAV_USER"}, "password": {"env": ENV}},
        include={"skills": ["skills/*/SKILL.md"]},
    )
    webdav.requests.clear()

    with pytest.raises(SourceError) as raised:
        materialise(source, tmp_path)

    assert "WEBDAV_USER" in str(raised.value)
    assert webdav.requests == []


def test_the_password_never_lands_under_the_cache(webdav, tmp_path):
    materialise(_source(webdav), tmp_path)

    written = [p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()]
    assert written
    assert not any(PASSWORD.encode() in blob for blob in written)


# -- the fingerprint -----------------------------------------------------------


def test_the_fingerprint_is_a_digest_of_every_files_etag(webdav, tmp_path):
    """An edit moves the digest and an addition moves the count with it."""
    source = _source(webdav)
    root = materialise(source, tmp_path)

    before = fingerprint(source, tmp_path, root)
    assert before["remote"] == root.name, "the copy is level with the folder"
    assert before["remote_files"] == 2
    assert fingerprint(source, tmp_path, root) == before, "an idle folder must compare equal"

    webdav.skill("x", "an edit that is longer than the first")
    edited = fingerprint(source, tmp_path, root)
    assert edited != before
    assert edited["remote_files"] == 2

    webdav.skill("y", "another skill")
    assert fingerprint(source, tmp_path, root)["remote_files"] == 3


def test_the_fingerprint_carries_the_digest_and_not_the_map(webdav, tmp_path):
    """It is published in /health and written to index.json, so it stays small.

    The map itself is the export's own file, which is where a live read reads
    it from -- not a thing to republish once per file per source.
    """
    source = _source(webdav)
    root = materialise(source, tmp_path)

    stamp = json.dumps(fingerprint(source, tmp_path, root))

    assert SKILL not in stamp and "docs/guide.md" not in stamp
    assert len(stamp) < 200
    assert json.loads((root / ETAGS_FILE).read_text()).keys() >= {SKILL}


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


def test_a_live_fetch_replaces_the_local_copy_rather_than_rewriting_it(
    webdav, tmp_path
):
    """The atomicity, pinned. A reader that already opened the file must read
    the bytes it opened all the way to the end -- which a rename gives it and a
    write over the same inode takes away."""
    source = _source(webdav)
    root = materialise(source, tmp_path)
    recorded = json.loads((root / ETAGS_FILE).read_text())[SKILL]
    webdav.skill("x", "edited in nextcloud, and very much longer than it was")

    with (root / SKILL).open("rb") as handle:
        fetch_file(source, root, SKILL, etag=recorded)
        held = handle.read().decode()

    assert "first" in held, "a reader holding the old file must read the old file"
    assert "edited in nextcloud" in _body(root)


def test_a_live_fetch_stages_its_download_outside_the_export(webdav, tmp_path):
    """A download the kernel kills leaves its temp file behind, and ``finally``
    does not run. Inside the export that file counts towards the export's
    extent, so the tree looks truncated for good -- and ``collect`` walks the
    source's home, so it could never reach one buried in a skill directory."""
    source = _source(webdav)
    root = materialise(source, tmp_path)
    recorded = json.loads((root / ETAGS_FILE).read_text())[SKILL]
    webdav.skill("x", "edited in nextcloud, and longer than before")
    staged = []

    class _Watched:
        """The real client, recording where each download is written."""

        def __init__(self, real):
            self._real = real

        def info(self, rel):
            return self._real.info(rel)

        def get_file(self, rel, local):
            staged.append(Path(local))
            return self._real.get_file(rel, local)

    fetch_file(source, root, SKILL, etag=recorded, fs=_Watched(client(source)))

    assert staged, "nothing was downloaded, so this test proved nothing"
    for tmp in staged:
        assert not tmp.is_relative_to(root)
        assert tmp.parent == root.parent / exports.WORK_DIR


def test_a_temp_file_a_killed_fetch_left_does_not_truncate_the_export(webdav, tmp_path):
    source = _source(webdav)
    root = materialise(source, tmp_path)
    (exports.workspace(root.parent) / ".tmp-killed").write_text("half a download")

    assert materialise(source, tmp_path) == root, "the export is still whole"


# -- names a URL has to carry --------------------------------------------------

AWKWARD = ("hash#.md", "question?.md", "with space.md", "café-筆記.md")


def test_a_folder_whose_names_need_encoding_is_copied_whole(webdav, tmp_path):
    """One ``#`` anywhere under the folder failed the entire source.

    webdav4 reads the listing's href through httpx, which decodes it, and then
    re-addresses that decoded path -- which httpx refuses for a ``#`` or a
    ``?``. Both are ordinary in a Nextcloud note's filename.
    """
    for name in AWKWARD:
        webdav.write(f"docs/{name}", f"body of {name}\n")
    webdav.write("docs/deep#dir/note?.md", "in a folder that needs it too\n")

    root = materialise(_source(webdav), tmp_path)

    for name in AWKWARD:
        assert (root / "docs" / name).read_text() == f"body of {name}\n"
    assert "in a folder" in (root / "docs" / "deep#dir" / "note?.md").read_text()


def test_a_name_that_needs_encoding_is_revalidated_like_any_other(webdav, tmp_path):
    """The live half of the same address, since it re-addresses one file."""
    rel = "docs/hash#and?both.md"
    webdav.write(rel, "first\n")
    source = _source(webdav)
    root = materialise(source, tmp_path)
    webdav.write(rel, "edited upstream, and rather longer than before\n")

    assert fetch_file(source, root, rel) is not None
    assert "edited upstream" in (root / rel).read_text()


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
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")

    assert [s.name for s in knowledge_base.index.visible()] == ["x"]
    assert knowledge_base.status["notes"]["status"] == "ok"
    assert "first" in knowledge_base.catalogue.read("skill://notes/x/SKILL.md")
    rows = "\n".join(str(entry) for entry in knowledge_base.catalogue.entries())
    assert "webdav" not in rows
    assert "127.0.0.1" not in rows
    assert "cache" not in rows
    assert ETAGS_FILE not in rows and VERSION_FILE not in rows
    assert knowledge_base.resources.files("notes") == ["docs/guide.md"]


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
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")

    assert knowledge_base.status["notes"]["status"] == "failed"
    assert knowledge_base.status["local"]["status"] == "ok"
    assert [s.name for s in knowledge_base.index.visible()] == ["y"]


def _local(tmp_path: Path) -> Path:
    root = tmp_path / "local"
    skill = root / "skills" / "y"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: y\ndescription: Local.\n---\n\nbody\n")
    return root


def test_a_folder_that_does_not_exist_is_named_as_that(webdav, tmp_path):
    """Not a copy failure with an empty path in it: the folder is not there."""
    source = _source(webdav).model_copy(update={"url": f"{webdav.url}/Agents"})

    with pytest.raises(SourceError) as raised:
        materialise(source, tmp_path)

    assert str(raised.value) == "notes: the folder /Agents does not exist"
    assert not isinstance(raised.value, AccessRefused)


def _served_config(webdav, tmp_path):
    return Config.model_validate(
        {
            "sources": [
                {
                    "name": "notes",
                    "url": webdav.url,
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


def test_refused_credentials_on_the_first_build_stop_the_server(
    webdav, tmp_path, monkeypatch
):
    """The failure a wait does not fix: exit, and be restarted until it is fixed."""
    monkeypatch.setenv(ENV, "not-the-password")

    with pytest.raises(AccessRefused, match=r"notes: .*HTTP 401"):
        KnowledgeBase(_served_config(webdav, tmp_path), tmp_path / "cache")


def test_main_exits_naming_the_source_whose_credentials_were_refused(
    webdav, tmp_path, monkeypatch, capsys
):
    from kubed.mcp_kb.main import main

    monkeypatch.setenv(ENV, "not-the-password")
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(_served_config(webdav, tmp_path).model_dump(mode="json"))
    )

    with pytest.raises(SystemExit) as raised:
        main(["serve", "--config", str(config), "--cache-dir", str(tmp_path / "c")])

    assert raised.value.code == 3
    err = capsys.readouterr().err
    assert "mcp-kb: notes: " in err and "HTTP 401: the credentials were refused" in err
    assert "not-the-password" not in err


def test_refused_credentials_on_a_refresh_leave_the_source_stale(
    webdav, tmp_path, monkeypatch
):
    """Already serving: keep serving, say why it stopped moving, do not exit."""
    knowledge_base = KnowledgeBase(_served_config(webdav, tmp_path), tmp_path / "cache")
    monkeypatch.setenv(ENV, "not-the-password")

    knowledge_base.refresh(force=True)

    notes = knowledge_base.status["notes"]
    assert notes["status"] == "stale"
    assert "HTTP 401: the credentials were refused" in notes["error"]
    assert "first" in knowledge_base.catalogue.read("skill://notes/x/SKILL.md")
