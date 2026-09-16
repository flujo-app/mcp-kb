"""Unit tests for the on-disk index: round trip, corruption, atomicity."""

import os
from pathlib import Path

import pytest

from mcp_school.config import load_config
from mcp_school.index import (
    INDEX_VERSION,
    Index,
    PromptRow,
    SkillRow,
    SourceRecord,
    config_hash,
    now,
)
from mcp_school.prompts import FilePrompt
from mcp_school.skills import Skill


def _skill(**overrides):
    fields = {
        "name": "alpha",
        "pack": "flatsource",
        "group": "flatsource",
        "description": "First skill.",
        "path": Path("/skills/flatsource/alpha"),
        "source": "flatsource",
        "tags": frozenset({"flatsource", "skill", "b", "a"}),
    }
    fields.update(overrides)
    return Skill(**fields)


def _source_record(**overrides):
    fields = {
        "name": "flatsource",
        "status": "ok",
        "library": "flatsource",
        "root": "/skills/flatsource",
        "fingerprint": {"mtime": 123.0},
        "built": now(),
        "error": None,
        "skills": (SkillRow.from_skill(_skill()),),
        "prompts": (
            PromptRow.of(
                Path("/skills/flatsource/debug.md"),
                FilePrompt(
                    name="flatsource_debug",
                    pack="flatsource",
                    source="flatsource",
                    template="Investigate.",
                    tags={"flatsource", "prompt", "b", "a"},
                ),
            ),
        ),
        "files": ("shared/logo.png",),
        "skill_dirs": ("/skills/flatsource/alpha",),
    }
    fields.update(overrides)
    return SourceRecord(**fields)


def _index(**overrides):
    fields = {
        "version": INDEX_VERSION,
        "built": now(),
        "config_hash": "deadbeef",
        "sources": {"flatsource": _source_record()},
    }
    fields.update(overrides)
    return Index(**fields)


@pytest.mark.unit
def test_an_index_round_trips_through_json(tmp_path):
    path = tmp_path / "index.json"
    written = _index()
    written.write(path)
    assert Index.read(path) == written


@pytest.mark.unit
def test_a_missing_or_corrupt_index_reads_as_none(tmp_path):
    missing = tmp_path / "missing.json"
    assert Index.read(missing) is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    assert Index.read(corrupt) is None


@pytest.mark.unit
def test_a_different_version_reads_as_none(tmp_path):
    path = tmp_path / "index.json"
    _index(version=INDEX_VERSION + 1).write(path)
    assert Index.read(path) is None


@pytest.mark.unit
def test_the_write_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "index.json"
    calls = []
    real_replace = os.replace

    def recording_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("mcp_school.index.os.replace", recording_replace)

    _index().write(path)

    assert [(str(src), str(dst)) for src, dst in calls] == [
        (str(path) + ".tmp", str(path))
    ]
    assert not (tmp_path / "index.json.tmp").exists()
    assert path.exists()


@pytest.mark.unit
def test_config_hash_ignores_key_order_and_changes_with_content(tmp_path):
    forward = tmp_path / "forward.yaml"
    forward.write_text(
        "libraries:\n- name: lib\n  tags: [x, y]\n"
        "sources:\n- name: lib\n  url: file:///skills/lib\n"
    )
    # Same content, top-level keys and per-source fields in the opposite order.
    reordered = tmp_path / "reordered.yaml"
    reordered.write_text(
        "sources:\n- url: file:///skills/lib\n  name: lib\n"
        "libraries:\n- tags: [x, y]\n  name: lib\n"
    )
    a = load_config(forward)
    b = load_config(reordered)
    assert config_hash(a) == config_hash(b)

    changed = tmp_path / "changed.yaml"
    changed.write_text(
        "libraries:\n- name: lib\n  tags: [x, y, z]\n"
        "sources:\n- name: lib\n  url: file:///skills/lib\n"
    )
    c = load_config(changed)
    assert config_hash(a) != config_hash(c)


@pytest.mark.unit
def test_a_skill_row_round_trips_to_a_skill(tmp_path):
    skill = _skill()
    assert SkillRow.from_skill(skill).to_skill() == skill
