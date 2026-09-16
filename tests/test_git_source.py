"""``git+…`` sources: clone, resolve a ref, export the tree, fingerprint it.

Every unit test here runs against a repository built in ``tmp_path`` and
addressed as ``git+file:///…``. No test in the suite may need github.com to be
reachable, so the one test that does is marked ``integration`` and skipped
unless ``MCP_SCHOOL_NETWORK`` is set.

libgit2's local transport refuses a shallow fetch outright, so these clones are
whole ones -- the depth is the one thing a ``git+file://`` repository cannot
prove, and the integration test is what covers it.
"""

import os
from types import SimpleNamespace

import pygit2
import pytest

from mcp_school import School
from mcp_school.config import Config, GitSource
from mcp_school.sources import SourceError, fingerprint, materialise
from mcp_school.sources.git import COMMIT_FILE, resolve

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

    assert root == tmp_path / "src" / "pack"
    assert "second" in _body(root)
    assert (root / "docs" / "guide.md").is_file()
    assert (tmp_path / "git" / "pack").is_dir()
    assert (root / COMMIT_FILE).read_text().strip() == origin.second


@pytest.mark.unit
def test_a_pinned_commit_is_exported_even_when_it_is_not_the_tip(origin, tmp_path):
    root = materialise(_source(origin, ref=origin.first), tmp_path)

    assert "first" in _body(root)
    assert (root / COMMIT_FILE).read_text().strip() == origin.first


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

    assert root == tmp_path / "src" / "pack" / "skills"
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


# -- the cache is only ever written through a rename ---------------------------


@pytest.mark.unit
def test_an_export_is_renamed_into_place(origin, tmp_path):
    """A half-written tree from a crashed export must not survive the next one."""
    dest = tmp_path / "src" / "pack"
    (dest / "leftover").mkdir(parents=True)
    (dest / "leftover" / "junk.md").write_text("from a previous life\n")
    half = tmp_path / "src" / "pack.tmp"
    half.mkdir(parents=True)
    (half / "partial.md").write_text("never finished\n")

    root = materialise(_source(origin), tmp_path)

    assert not (root / "leftover").exists()
    assert not half.exists()
    assert not (tmp_path / "git" / "pack.tmp").exists()
    assert "second" in _body(root)


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

    assert before == {"commit": origin.second, "ref": "main", "remote": origin.second}

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
    school = School(config, tmp_path / "cache")

    assert [s.name for s in school.index.visible()] == ["x"]
    assert school.status["pack"]["status"] == "ok"
    rows = "\n".join(str(entry) for entry in school.catalogue.entries())
    assert COMMIT_FILE not in rows
    assert "cache" not in rows
    assert school.resources.files("pack") == ["docs/guide.md"]


# -- the real thing ------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("MCP_SCHOOL_NETWORK"), reason="needs GitHub over the network"
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
