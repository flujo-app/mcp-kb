"""``git+…`` sources: clone, resolve a ref, export the tree, fingerprint it.

Every unit test here runs against a repository built in ``tmp_path`` and
addressed as ``git+file:///…``. No test in the suite may need github.com to be
reachable, so the one test that does is marked ``integration`` and skipped
unless ``MCP_KB_NETWORK`` is set.

libgit2's local transport refuses a shallow fetch outright, so a ``git+file://``
clone is a whole one and cannot show either the depth or a credential -- a local
remote never asks for one. The ``private`` fixture below closes both gaps with a
real smart-HTTP remote behind Basic auth, served out of ``git-http-backend`` on
a loopback port. Still no network.
"""

import base64
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pygit2
import pytest

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.config import Config, GitSource
from kubed.mcp_kb.sources import SourceError, fingerprint, materialise
from kubed.mcp_kb.sources import export as exports
from kubed.mcp_kb.sources.git import COMMIT_FILE, resolve

SIGNATURE = pygit2.Signature("Test", "test@example.com", 1700000000, 0)


def _commit(repo, root, body, parents, message):
    (root / "skills" / "x").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "x" / "SKILL.md").write_text(
        f"---\nname: x\ndescription: A skill.\n---\n\n{body}\n"
    )
    repo.index.add_all()
    repo.index.write()
    return repo.create_commit(
        "refs/heads/main", SIGNATURE, SIGNATURE, message, repo.index.write_tree(), parents
    )


@pytest.fixture
def origin(tmp_path_factory):
    """A two-commit repository with a tag on the first, as ``git+file:///…``.

    ``skills/x/SKILL.md`` says ``first`` at ``v1`` and ``second`` at the tip,
    which is how a test tells which commit was actually exported.
    """
    root = tmp_path_factory.mktemp("origin")
    repo = pygit2.init_repository(str(root), bare=False, initial_head="main")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("pack-level guidance\n")
    first = _commit(repo, root, "first", [], "one")
    repo.create_reference("refs/tags/v1", first)
    second = _commit(repo, root, "second", [first], "two")
    return SimpleNamespace(
        path=root,
        repo=repo,
        url=f"git+file://{root}",
        first=str(first),
        second=str(second),
    )


def _source(origin, **kwargs):
    return GitSource(name="pack", url=origin.url, **kwargs)


def _config(origin, **kwargs):
    """A one-source config over ``origin``, for the tests that need a whole server."""
    return Config.model_validate(
        {
            "sources": [
                {
                    "name": "pack",
                    "url": origin.url,
                    "include": {"skills": ["skills/*/SKILL.md"]},
                    **kwargs,
                }
            ]
        }
    )


def _body(root):
    return (root / "skills" / "x" / "SKILL.md").read_text()


def _advance(origin, body="third"):
    """Another commit on ``main``, so the remote tip moves under the export."""
    head = origin.repo.head.target
    return str(_commit(origin.repo, origin.path, body, [head], "three"))


# -- materialise ---------------------------------------------------------------


@pytest.mark.unit
def test_a_git_source_exports_the_tree_at_the_default_branch(origin, tmp_path):
    root = materialise(_source(origin), tmp_path)

    assert root == tmp_path / "src" / "pack" / origin.second
    assert "second" in _body(root)
    assert (root / "docs" / "guide.md").is_file()
    assert (tmp_path / "git" / "pack").is_dir()
    assert (root / COMMIT_FILE).read_text().split()[0] == origin.second


@pytest.mark.unit
def test_a_pinned_commit_is_exported_even_when_it_is_not_the_tip(origin, tmp_path):
    root = materialise(_source(origin, ref=origin.first), tmp_path)

    assert "first" in _body(root)
    assert root.name == origin.first
    assert (root / COMMIT_FILE).read_text().split()[0] == origin.first


@pytest.mark.unit
def test_a_tag_and_a_branch_resolve(origin, tmp_path):
    tagged = _source(origin, ref="v1")
    assert "first" in _body(materialise(tagged, tmp_path))
    assert resolve(tagged, tmp_path) == origin.first

    branch = _source(origin, ref="main")
    assert "second" in _body(materialise(branch, tmp_path))
    assert resolve(branch, tmp_path) == origin.second


@pytest.mark.unit
def test_an_unknown_ref_is_a_source_error(origin, tmp_path):
    with pytest.raises(SourceError, match="'nope' not found"):
        materialise(_source(origin, ref="nope"), tmp_path)


@pytest.mark.unit
def test_an_unknown_commit_is_a_source_error(origin, tmp_path):
    with pytest.raises(SourceError, match="is not in"):
        materialise(_source(origin, ref="0" * 40), tmp_path)


@pytest.mark.unit
def test_a_subdirectory_narrows_the_harvest_root(origin, tmp_path):
    root = materialise(_source(origin, subdirectory="skills"), tmp_path)

    assert root == tmp_path / "src" / "pack" / origin.second / "skills"
    assert (root / "x" / "SKILL.md").is_file()


@pytest.mark.unit
def test_a_subdirectory_that_is_not_in_the_tree_is_a_source_error(origin, tmp_path):
    with pytest.raises(SourceError, match="subdirectory 'nowhere'"):
        materialise(_source(origin, subdirectory="nowhere"), tmp_path)


@pytest.mark.unit
def test_an_unreachable_remote_is_a_source_error(tmp_path):
    source = GitSource(name="pack", url=f"git+file://{tmp_path / 'nothing-here'}")

    with pytest.raises(SourceError, match="clone failed"):
        materialise(source, tmp_path / "cache")


# -- an export is never written where one is being served ----------------------


@pytest.mark.unit
def test_a_new_export_leaves_the_old_one_whole(origin, tmp_path):
    """The record a live snapshot holds names a tree; a refresh must not touch it.

    Each commit gets its own directory, so the export a reader is part way
    through is never the export the refresh is writing.
    """
    source = _source(origin, ref="main")
    old = materialise(source, tmp_path)
    _advance(origin)

    new = materialise(source, tmp_path)

    assert new != old
    assert "second" in _body(old)
    assert "third" in _body(new)


@pytest.mark.unit
def test_a_read_in_flight_survives_a_refresh_that_moved_the_ref(origin, tmp_path):
    """server.py's contract, for a git source: a request already in flight
    finishes against the catalogue it started with."""
    knowledge_base = KnowledgeBase(_config(origin, ref="main"), tmp_path / "cache")
    serving = knowledge_base.snapshot
    _advance(origin)

    assert knowledge_base.refresh() == ["pack"]
    assert "second" in serving.catalogue.read("skill://pack/x")
    assert "third" in knowledge_base.catalogue.read("skill://pack/x")


@pytest.mark.unit
def test_an_export_is_complete_before_it_is_visible(origin, tmp_path, monkeypatch):
    """The rename is the last thing that happens.

    Nothing writes into a commit directory after it exists, so the instant one
    appears it already holds the whole tree and the stamp that says so -- one
    fact rather than two, which is what makes a crash recoverable.
    """
    real = Path.rename
    whole = {}

    def spy(self, target):
        stamp = self / COMMIT_FILE
        skill = self / "skills" / "x" / "SKILL.md"
        whole[str(target)] = stamp.is_file() and skill.is_file()
        return real(self, target)

    monkeypatch.setattr(Path, "rename", spy)
    root = materialise(_source(origin), tmp_path)

    assert whole[str(root)] is True


@pytest.mark.unit
def test_a_stamped_export_missing_its_files_is_rebuilt(origin, tmp_path):
    """An interrupted delete leaves the stamp behind -- it is not proof enough.

    This is the tree that used to be reused forever: the stamp named the right
    commit, the directory existed, and the skills were gone.
    """
    source = _source(origin)
    root = materialise(source, tmp_path)
    (root / "skills" / "x" / "SKILL.md").unlink()

    again = materialise(source, tmp_path)

    assert "second" in _body(again)


@pytest.mark.unit
def test_a_truncated_export_is_rebuilt_across_a_restart(origin, tmp_path):
    """The crash half of it, end to end: a restart must not report `ok` over a
    tree whose bodies are gone."""
    cache = tmp_path / "cache"
    config = _config(origin, ref="main")
    KnowledgeBase(config, cache)
    export = next((cache / "src" / "pack").iterdir())
    (export / "skills" / "x" / "SKILL.md").unlink()

    restarted = KnowledgeBase(config, cache)
    restarted.refresh()

    assert restarted.status["pack"]["status"] == "ok"
    assert "second" in restarted.catalogue.read("skill://pack/x")


@pytest.mark.unit
def test_a_rebuild_never_interrupts_a_reader_of_the_served_tree(origin, tmp_path):
    """Measured on this branch as 524 failed reads in 14 634; now none.

    A commit that has not moved exports to the same directory, so repairing an
    export that lost files means rebuilding at the path the live snapshot is
    serving out of. The repair is right; doing it there is not.
    """
    source = _source(origin, ref="main")
    root = materialise(source, tmp_path)
    served = root / "skills" / "x" / "SKILL.md"
    errors: list[str] = []
    stop = threading.Event()

    def read():
        while not stop.is_set():
            try:
                if "second" not in served.read_text():
                    errors.append("torn")
            except OSError as exc:
                errors.append(type(exc).__name__)

    readers = [threading.Thread(target=read, daemon=True) for _ in range(6)]
    for reader in readers:
        reader.start()
    try:
        current = root
        for round_ in range(4):
            # A file the stamp does not account for: the export no longer holds
            # the commit it claims, which is what makes the next pass rebuild.
            (current / f"unaccounted{round_}.md").write_text("not in the stamp\n")
            current = materialise(source, tmp_path)
            time.sleep(0.05)
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=5)

    assert errors == []
    assert "second" in _body(root), "the tree the readers held is still whole"


@pytest.mark.unit
def test_a_crashed_export_leaves_nothing_at_a_commit_path(origin, tmp_path):
    """What a crash may leave behind is an unreferenced work directory, and
    never a half tree at the name of the commit a later export would trust."""
    source = _source(origin)
    home = tmp_path / "src" / "pack"
    half = home / (exports.WORK_PREFIX + origin.second)
    (half / "skills").mkdir(parents=True)
    (half / "skills" / "leftover.md").write_text("never finished\n")

    root = materialise(source, tmp_path)

    assert not half.exists()
    assert not (root / "skills" / "leftover.md").exists()
    assert "second" in _body(root)


@pytest.mark.unit
def test_superseded_exports_are_collected_once_nothing_can_be_reading_them(
    origin, tmp_path
):
    """The cache holds the commits a source moved through recently, not every
    commit it ever saw -- but the one the previous snapshot was built against
    stays, however old it is."""
    source = _source(origin, ref="main")
    oldest = materialise(source, tmp_path)
    _advance(origin, "third")
    previous = materialise(source, tmp_path)
    _advance(origin, "fourth")
    for export in (oldest, previous):
        aged = export.stat().st_mtime - exports.GRACE_SECONDS - 60
        os.utime(export, (aged, aged))

    newest = materialise(source, tmp_path)

    assert sorted(p.name for p in (tmp_path / "src" / "pack").iterdir()) == sorted(
        [previous.name, newest.name]
    )


@pytest.mark.unit
def test_a_second_materialise_reuses_the_clone(origin, tmp_path, monkeypatch):
    source = _source(origin)
    materialise(source, tmp_path)

    def must_not_clone(*args, **kwargs):
        raise AssertionError("the clone must not be repeated")

    monkeypatch.setattr(pygit2, "clone_repository", must_not_clone)

    assert "second" in _body(materialise(source, tmp_path))


@pytest.mark.unit
def test_an_export_is_not_repeated_for_a_commit_already_exported(origin, tmp_path):
    """/reindex must not delete and rewrite a tree the snapshot is serving."""
    source = _source(origin)
    root = materialise(source, tmp_path)
    (root / "skills" / "x" / "SKILL.md").write_text("proof this file was not rewritten")

    materialise(source, tmp_path)

    assert _body(root) == "proof this file was not rewritten"


# -- fingerprint ---------------------------------------------------------------


@pytest.mark.unit
def test_the_fingerprint_is_the_exported_commit_and_moves_with_the_tip(
    origin, tmp_path
):
    source = _source(origin, ref="main")
    root = materialise(source, tmp_path)
    before = fingerprint(source, tmp_path, root)

    assert before == {
        "commit": origin.second,
        "files": 2,
        "ref": "main",
        "remote": origin.second,
    }

    third = _advance(origin)
    after = fingerprint(source, tmp_path, root)

    assert after != before
    assert after["remote"] == third
    assert "third" in _body(materialise(source, tmp_path))


@pytest.mark.unit
def test_the_fingerprint_of_the_default_branch_follows_head(origin, tmp_path):
    source = _source(origin)
    root = materialise(source, tmp_path)

    assert fingerprint(source, tmp_path, root)["remote"] == origin.second
    third = _advance(origin)
    assert fingerprint(source, tmp_path, root)["remote"] == third


@pytest.mark.unit
def test_a_pinned_sha_fingerprints_without_touching_the_remote(
    origin, tmp_path, monkeypatch
):
    """A pinned commit cannot move, so asking the remote about it is pure cost."""
    source = _source(origin, ref=origin.first)
    root = materialise(source, tmp_path)

    def must_not_connect(*args, **kwargs):
        raise AssertionError("a pinned sha must not reach the remote")

    monkeypatch.setattr(pygit2.Remote, "connect", must_not_connect)
    monkeypatch.setattr(pygit2.Remote, "list_heads", must_not_connect)

    assert fingerprint(source, tmp_path, root) == {
        "commit": origin.first,
        "files": 2,
        "ref": origin.first,
        "remote": origin.first,
    }


@pytest.mark.unit
def test_a_ref_that_vanished_from_the_remote_is_a_source_error(origin, tmp_path):
    source = _source(origin, ref="v1")
    root = materialise(source, tmp_path)
    origin.repo.references["refs/tags/v1"].delete()

    with pytest.raises(SourceError, match="'v1' not found"):
        fingerprint(source, tmp_path, root)


# -- credentials ---------------------------------------------------------------


@pytest.mark.unit
def test_a_token_is_resolved_from_the_environment_and_never_written_down(
    origin, tmp_path, monkeypatch
):
    """The remote saved in the clone is the URL from the config, credentials apart."""
    monkeypatch.setenv("GIT_TOKEN", "ghp-not-a-real-token")
    source = _source(
        origin,
        auth={"username": "x-access-token", "password": {"env": "GIT_TOKEN"}},
    )

    materialise(source, tmp_path)

    written = [
        p.read_bytes()
        for p in (tmp_path / "git" / "pack").rglob("*")
        if p.is_file()
    ]
    assert not any(b"ghp-not-a-real-token" in blob for blob in written)


@pytest.mark.unit
def test_an_unset_credential_is_a_source_error(origin, tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_TOKEN", raising=False)
    source = _source(
        origin,
        auth={"username": "x-access-token", "password": {"env": "GIT_TOKEN"}},
    )

    with pytest.raises(SourceError, match="GIT_TOKEN"):
        materialise(source, tmp_path)


# -- through the catalogue -----------------------------------------------------


@pytest.mark.unit
def test_a_git_source_is_served_without_naming_git_anywhere(origin, tmp_path):
    """A backend is config-only: nothing it leaves in the cache may be served."""
    config = Config.model_validate(
        {
            "sources": [
                {
                    "name": "pack",
                    "url": origin.url,
                    "include": {"skills": ["skills/*/SKILL.md"], "files": ["**/*"]},
                }
            ]
        }
    )
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")

    assert [s.name for s in knowledge_base.index.visible()] == ["x"]
    assert knowledge_base.status["pack"]["status"] == "ok"
    rows = "\n".join(str(entry) for entry in knowledge_base.catalogue.entries())
    assert COMMIT_FILE not in rows
    assert "cache" not in rows
    assert origin.second not in rows
    assert knowledge_base.resources.files("pack") == ["docs/guide.md"]


# -- a remote that actually authenticates --------------------------------------

USERNAME = "x-access-token"
PASSWORD = "s3cret-not-a-real-token"
CREDENTIAL = {"username": USERNAME, "password": {"env": "GIT_TOKEN"}}


def _backend():
    """``git-http-backend``, or None where git is not installed."""
    try:
        done = subprocess.run(
            ["git", "--exec-path"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git
        return None
    backend = Path(done.stdout.strip()) / "git-http-backend"
    return backend if backend.is_file() else None  # pragma: no cover - no git


def _cgi_handler(root, backend):
    """A smart-HTTP git server that answers 401 until it is given the password."""
    expected = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            """Quiet: the default writes every request to stderr."""

        def _serve(self):
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            length = int(self.headers.get("Content-Length") or 0)
            done = subprocess.run(
                [str(backend)],
                input=self.rfile.read(length),
                capture_output=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "GIT_PROJECT_ROOT": str(root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": path,
                    "QUERY_STRING": query,
                    "REMOTE_USER": USERNAME,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                },
            )
            head, _, body = done.stdout.partition(b"\r\n\r\n")
            self.send_response(500 if done.returncode else 200)
            for line in head.splitlines():
                key, _, value = line.partition(b":")
                if key.lower() != b"status":
                    self.send_header(key.decode(), value.strip().decode())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _serve
        do_POST = _serve

    return Handler


@pytest.fixture
def private(tmp_path_factory):
    """The two-commit repository again, behind HTTP Basic auth on a loopback port.

    The only remote in the suite that asks for a credential, which is what makes
    it the only one that can prove the credential is actually supplied -- and
    the only one libgit2 will clone shallow.
    """
    backend = _backend()
    if backend is None:  # pragma: no cover - git is installed everywhere this runs
        pytest.skip("git-http-backend is not installed")
    home = tmp_path_factory.mktemp("private")
    work = home / "work"
    repo = pygit2.init_repository(str(work), bare=False, initial_head="main")
    first = _commit(repo, work, "first", [], "one")
    second = _commit(repo, work, "second", [first], "two")
    bare = pygit2.clone_repository(str(work), str(home / "priv.git"), bare=True)
    # A stock server answers a fetch only for a ref it advertises; GitHub answers
    # for any commit it holds, which is what an older pin needs from a depth-1
    # clone. This is the same permission, turned on.
    bare.config["uploadpack.allowAnySHA1InWant"] = True
    bare.free()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _cgi_handler(home, backend))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(
            url=f"git+http://127.0.0.1:{server.server_address[1]}/priv.git",
            first=str(first),
            second=str(second),
        )
    finally:
        server.shutdown()
        server.server_close()


def _private(private, **kwargs):
    return GitSource(name="pack", url=private.url, auth=CREDENTIAL, **kwargs)


@pytest.mark.unit
def test_a_private_remote_resolves_a_branch_it_has_to_authenticate_for(
    private, tmp_path, monkeypatch
):
    """The credential has to reach the ref advertisement, not only the clone.

    A branch, a tag and an unset ref all ask the remote what it points at on
    every check. Drop the credentials there and a private repo builds only when
    it is pinned to a sha, which is the one case that never asks.
    """
    monkeypatch.setenv("GIT_TOKEN", PASSWORD)
    source = _private(private, ref="main")

    root = materialise(source, tmp_path)

    assert "second" in _body(root)
    assert fingerprint(source, tmp_path, root)["remote"] == private.second
    assert resolve(source, tmp_path) == private.second


@pytest.mark.unit
def test_a_private_remote_with_no_ref_resolves_head(private, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_TOKEN", PASSWORD)
    source = _private(private)

    root = materialise(source, tmp_path)

    assert fingerprint(source, tmp_path, root)["remote"] == private.second


@pytest.mark.unit
def test_a_wrong_credential_fails_without_naming_the_secret(
    private, tmp_path, monkeypatch
):
    monkeypatch.setenv("GIT_TOKEN", "wrong-" + PASSWORD)

    with pytest.raises(SourceError) as raised:
        materialise(_private(private), tmp_path)

    assert "wrong-" not in str(raised.value)


@pytest.mark.unit
def test_a_remote_is_cloned_bare_and_shallow(private, tmp_path, monkeypatch):
    """Bare and shallow is the whole cost story, and only a real transport shows
    it: libgit2 refuses a shallow fetch over ``file://``."""
    monkeypatch.setenv("GIT_TOKEN", PASSWORD)

    materialise(_private(private), tmp_path)

    clone = tmp_path / "git" / "pack"
    assert pygit2.Repository(str(clone)).is_bare
    assert not (clone / ".git").exists()
    assert (clone / "shallow").is_file()


@pytest.mark.unit
def test_a_pin_below_the_shallow_tip_is_fetched_by_sha(private, tmp_path, monkeypatch):
    """The one commit a depth-1 clone holds is the tip. An older pin is the
    reason ``ref`` is a field and not part of the URL."""
    monkeypatch.setenv("GIT_TOKEN", PASSWORD)

    root = materialise(_private(private, ref=private.first), tmp_path)

    assert "first" in _body(root)


# -- the real thing ------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("MCP_KB_NETWORK"), reason="needs GitHub over the network"
)
def test_github_shorthand_against_the_real_thing(tmp_path):
    source = GitSource(
        name="superpowers",
        url="github://obra/superpowers",
        ref="b36e0825a2f1c0e2c0b4d7a0e3c7b1f2a4d6e8c0",
    )
    with pytest.raises(SourceError):
        # A sha that does not exist: proves the pin is fetched, not guessed.
        materialise(source, tmp_path)

    floating = GitSource(name="superpowers", url="github://obra/superpowers")
    root = materialise(floating, tmp_path)

    assert (root / "skills").is_dir()
    stamp = fingerprint(floating, tmp_path, root)
    assert stamp["commit"] == stamp["remote"]

    # The GitHub-specific path the plan singles out: a pin far below the tip,
    # which a depth-1 clone cannot contain and has to fetch by sha. A floating
    # HEAD never exercises it, and neither does a pin that happens to be HEAD.
    old = GitSource(
        name="superpowers",
        url="github://obra/superpowers",
        ref="00029480418050a896d8b41e9f10cae8bb4320ab",
    )
    older = materialise(old, tmp_path)

    assert older != root
    assert fingerprint(old, tmp_path, older)["commit"] == old.ref
