"""Materialising a config source into a local directory, and its failure mode."""

import os

import pytest

from mcp_school.config import Config, FileSource
from mcp_school.sources import SourceError, fingerprint, materialise, materialise_all


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
