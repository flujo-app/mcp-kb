"""Materialising a config source into a local directory, and its failure mode."""

import os

import pytest

from kubed.mcp_kb.config import Config, FileSource
from kubed.mcp_kb.sources import SourceError, fingerprint, materialise, materialise_all
from kubed.mcp_kb.sources.file import fingerprint_file


def test_a_file_source_is_served_in_place(tmp_path):
    source = FileSource(name="kubed", url=f"file://{tmp_path}")
    assert materialise(source, tmp_path / "cache") == tmp_path
    assert not (tmp_path / "cache").exists()


def test_a_missing_directory_is_a_source_error(tmp_path):
    source = FileSource(name="kubed", url=f"file://{tmp_path / 'nope'}")
    with pytest.raises(SourceError, match="nope"):
        materialise(source, tmp_path / "cache")


def test_materialise_all_skips_a_broken_source_and_keeps_the_rest(tmp_path):
    (tmp_path / "good").mkdir()
    config = Config.model_validate(
        {
            "sources": [
                {"name": "good", "url": f"file://{tmp_path / 'good'}"},
                {"name": "bad", "url": f"file://{tmp_path / 'bad'}"},
            ]
        }
    )
    got = materialise_all(config, tmp_path / "cache")
    assert got["good"] == tmp_path / "good"
    assert isinstance(got["bad"], SourceError)


def test_a_fingerprint_counts_files_bytes_and_newest_mtime(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "b.txt").write_text("hello")
    source = FileSource(name="kubed", url=f"file://{tmp_path}")
    fp = fingerprint(source, tmp_path / "cache", tmp_path)
    assert fp["files"] == 2
    assert fp["bytes"] == len("hi") + len("hello")
    assert fp["newest"] == max(
        (tmp_path / "a.txt").stat().st_mtime_ns,
        (tmp_path / "b.txt").stat().st_mtime_ns,
    )


def test_a_fingerprint_changes_when_a_file_gets_a_newer_mtime(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hi")
    source = FileSource(name="kubed", url=f"file://{tmp_path}")
    before = fingerprint(source, tmp_path / "cache", tmp_path)

    later_ns = before["newest"] + 5_000_000_000
    os.utime(target, ns=(later_ns, later_ns))
    after = fingerprint(source, tmp_path / "cache", tmp_path)

    assert after["newest"] > before["newest"]
    assert after["files"] == before["files"]
    assert after["bytes"] == before["bytes"]


def test_a_fingerprint_changes_when_a_file_is_added(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    source = FileSource(name="kubed", url=f"file://{tmp_path}")
    before = fingerprint(source, tmp_path / "cache", tmp_path)

    (tmp_path / "b.txt").write_text("more")
    after = fingerprint(source, tmp_path / "cache", tmp_path)

    assert after["files"] == before["files"] + 1
    assert after["bytes"] == before["bytes"] + len("more")


def test_a_fingerprint_is_unchanged_when_nothing_changed(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    source = FileSource(name="kubed", url=f"file://{tmp_path}")
    assert fingerprint(source, tmp_path / "cache", tmp_path) == fingerprint(
        source, tmp_path / "cache", tmp_path
    )


def test_a_fingerprint_skips_hidden_directories_except_conventional_ones(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    skill_dir = tmp_path / ".github" / "skills" / "a"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: a\n---\n")

    source = FileSource(name="kubed", url=f"file://{tmp_path}")
    fp = fingerprint(source, tmp_path / "cache", tmp_path)

    assert fp["files"] == 1


def test_a_fingerprint_does_not_follow_a_symlink_out_of_the_tree(tmp_path):
    """A link is not a file the source owns; counting it counts foreign bytes."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("hi")
    outside = tmp_path / "outside.txt"
    outside.write_text("a much longer file that lives somewhere else")

    source = FileSource(name="kubed", url=f"file://{root}")
    before = fingerprint(source, tmp_path / "cache", root)

    (root / "link.txt").symlink_to(outside)
    after = fingerprint(source, tmp_path / "cache", root)

    assert after == before


@pytest.mark.unit
def test_a_configmap_update_moves_the_fingerprint(tmp_path):
    """A ConfigMap mount is entirely symlinks, and updating one re-points `..data`.

    Refusing every symlink made such a mount fingerprint as empty — identical
    before and after an update — so `refresh:` never rebuilt it and the index
    kept paths into the timestamp directory Kubernetes had just deleted. An
    in-root symlink is followed now, matching `harvest.files`.
    """
    root = tmp_path / "prompts"
    first = root / "..2026_09_16_13_15_49"
    first.mkdir(parents=True)
    (first / "debug-logs.md").write_text("version one")
    (root / "..data").symlink_to(first)
    (root / "debug-logs.md").symlink_to(root / "..data" / "debug-logs.md")

    before = fingerprint_file(None, None, root)
    assert before["files"] == 1

    second = root / "..2026_09_16_99_99_99"
    second.mkdir()
    (second / "debug-logs.md").write_text("version two, rather longer")
    (root / "..data").unlink()
    (root / "..data").symlink_to(second)

    assert fingerprint_file(None, None, root) != before


@pytest.mark.unit
def test_a_symlink_out_of_the_root_stays_out_of_the_fingerprint(tmp_path):
    """The reason every symlink was refused in the first place.

    An edit to a file elsewhere on the disk must not rebuild this source.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "big.md"
    target.write_text("x")
    root = tmp_path / "src"
    root.mkdir()
    (root / "linked.md").symlink_to(target)

    before = fingerprint_file(None, None, root)
    target.write_text("x" * 5000)

    assert before["files"] == 0
    assert fingerprint_file(None, None, root) == before


@pytest.mark.unit
def test_an_update_beneath_a_symlinked_directory_moves_the_fingerprint(tmp_path):
    """A volume `items[].path` like `shared/foo.md` mounts `shared -> ..data/shared`.

    `os.walk` lists that link as a directory and, not following links, never
    entered it — so the fingerprint was empty before and after an update while
    harvest went on serving the file.
    """
    root = tmp_path / "pack"
    first = root / "..2026_a"
    (first / "shared").mkdir(parents=True)
    (first / "shared" / "foo.md").write_text("one")
    (root / "..data").symlink_to(first)
    (root / "shared").symlink_to(root / "..data" / "shared")

    before = fingerprint_file(None, None, root)
    assert before["files"] == 1

    second = root / "..2026_b"
    (second / "shared").mkdir(parents=True)
    (second / "shared" / "foo.md").write_text("two, and longer")
    (root / "..data").unlink()
    (root / "..data").symlink_to(second)

    assert fingerprint_file(None, None, root) != before


@pytest.mark.unit
def test_a_directory_link_loop_or_escape_is_not_walked(tmp_path):
    """Following directory links must not loop, nor reach outside the root."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "big.md").write_text("x")
    root = tmp_path / "src"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.md").write_text("a")
    (root / "real" / "up").symlink_to(root)
    (root / "out").symlink_to(outside)

    assert fingerprint_file(None, None, root)["files"] == 1
