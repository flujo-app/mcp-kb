"""Unit tests for the on-disk index: round trip, corruption, atomicity."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from kubed.mcp_kb.catalogue.index import (
    INDEX_VERSION,
    FetchRecord,
    Index,
    PluginRecord,
    PromptRow,
    SkillRow,
    config_hash,
    now,
)
from kubed.mcp_kb.catalogue.skills import Skill
from kubed.mcp_kb.config import load_config

FETCH = "file:///skills/flatsource"


def _skill(**overrides):
    fields = {
        "name": "alpha",
        "library": "flatsource",
        "folder": "",
        "description": "First skill.",
        "path": Path("/skills/flatsource/alpha"),
        "plugin": "flatsource",
        "root": Path("/skills/flatsource"),
        "category": "ops",
        "tags": frozenset({"b", "a"}),
    }
    fields.update(overrides)
    return Skill(**fields)


def _fetch_record(**overrides):
    fields = {
        "key": FETCH,
        "status": "ok",
        "root": "/skills/flatsource",
        "fingerprint": {"mtime": 123.0},
        "built": now(),
        "error": None,
    }
    fields.update(overrides)
    return FetchRecord(**fields)


def _plugin_record(**overrides):
    fields = {
        "id": "flatsource",
        "fetch": FETCH,
        "root": "/skills/flatsource",
        "status": "ok",
        "error": None,
        "built": now(),
        "skills": (SkillRow.from_skill(_skill()),),
        "prompts": (PromptRow(path="/skills/flatsource/debug.md", dialect="claude"),),
        "files": ("shared/logo.png",),
        "skill_dirs": ("/skills/flatsource/alpha",),
    }
    fields.update(overrides)
    return PluginRecord(**fields)


def _index(**overrides):
    fields = {
        "version": INDEX_VERSION,
        "built": now(),
        "config_hash": "deadbeef",
        "fetches": {FETCH: _fetch_record()},
        "plugins": {"flatsource": _plugin_record()},
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
@pytest.mark.parametrize("version", [INDEX_VERSION + 1, 4, 3, 2, 1])
def test_a_different_version_reads_as_none(tmp_path, version):
    """Older as well as newer, by literal and not only by offset.

    An index written by an earlier release is the one a real upgrade meets, and
    the literals are the shapes before this one (1 on main, 2 part-way through
    the address-space work, 3 before a record carried `skipped`, 4 the
    per-source shape before plugins): lowering `INDEX_VERSION` back to any of
    them must fail here, which an offset from the current value alone never
    would.
    """
    path = tmp_path / "index.json"
    _index(version=version).write(path)
    assert Index.read(path) is None


@pytest.mark.unit
def test_a_v4_index_on_disk_is_discarded_whole(tmp_path):
    """The shape a running deployment upgrades from: one `sources` mapping.

    Not a version literal alone -- the file as v4 actually wrote it, so a
    reader that grew lenient about the mapping names would still be caught.
    """
    path = tmp_path / "index.json"
    path.write_text(
        json.dumps(
            {
                "version": 4,
                "built": now(),
                "config_hash": "x",
                "sources": {
                    "flatsource": {
                        "name": "flatsource",
                        "status": "ok",
                        "library": "flatsource",
                        "root": "/skills/flatsource",
                        "fingerprint": {},
                        "built": now(),
                        "error": None,
                        "skills": [],
                        "prompts": [],
                        "files": [],
                        "skill_dirs": [],
                        "skipped": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert Index.read(path) is None


@pytest.mark.unit
@pytest.mark.parametrize("field", ["fetches", "plugins"])
def test_a_wrong_shaped_mapping_reads_as_none(tmp_path, field):
    path = tmp_path / "index.json"
    raw = {
        "version": INDEX_VERSION,
        "built": now(),
        "config_hash": "x",
        "fetches": {},
        "plugins": {},
    }
    raw[field] = []
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert Index.read(path) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "row", [{"reason": "no path"}, {"path": 1, "reason": "x"}, "prompts/bad.md"]
)
def test_a_malformed_skipped_row_reads_as_none(tmp_path, row):
    """A half-formed row would be indexed into at boot; the index is rebuilt instead."""
    path = tmp_path / "index.json"
    _index().write(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["plugins"]["flatsource"]["skipped"] = [row]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert Index.read(path) is None


@pytest.mark.unit
def test_the_write_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "index.json"
    calls = []
    real_replace = os.replace

    def recording_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("kubed.mcp_kb.catalogue.index.os.replace", recording_replace)

    _index().write(path)

    assert len(calls) == 1
    src, dst = calls[0]
    assert Path(src).parent == tmp_path
    assert Path(dst) == path
    assert not Path(src).exists()  # renamed away by os.replace
    assert path.exists()


@pytest.mark.unit
def test_two_writers_in_the_same_directory_do_not_share_a_temp_name(
    tmp_path, monkeypatch
):
    """M4: a fixed `.tmp` sibling races across processes; `mkstemp` cannot."""
    path = tmp_path / "index.json"
    names = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        names.append(name)
        return fd, name

    monkeypatch.setattr("kubed.mcp_kb.catalogue.index.tempfile.mkstemp", recording_mkstemp)

    _index().write(path)
    _index().write(path)

    assert len(names) == 2
    assert names[0] != names[1]


@pytest.mark.unit
def test_config_hash_ignores_key_order_and_changes_with_content(tmp_path):
    forward = tmp_path / "forward.yaml"
    forward.write_text(
        "plugins:\n- name: lib\n  source: file:///skills/lib\n  tags: [x, y]\n"
        "libraries:\n- name: lib\n  plugins: [lib]\n"
    )
    # Same content, top-level keys and per-plugin fields in the opposite order.
    reordered = tmp_path / "reordered.yaml"
    reordered.write_text(
        "libraries:\n- plugins: [lib]\n  name: lib\n"
        "plugins:\n- tags: [x, y]\n  source: file:///skills/lib\n  name: lib\n"
    )
    a = load_config(forward)
    b = load_config(reordered)
    assert config_hash(a) == config_hash(b)

    changed = tmp_path / "changed.yaml"
    changed.write_text(
        "plugins:\n- name: lib\n  source: file:///skills/lib\n  tags: [x, y, z]\n"
        "libraries:\n- name: lib\n  plugins: [lib]\n"
    )
    c = load_config(changed)
    assert config_hash(a) != config_hash(c)


@pytest.mark.unit
def test_a_skill_row_round_trips_to_a_skill(tmp_path):
    """The row carries the skill's own fields; the library and the plugin's
    labels are put back on by whoever assigns the plugin to a library."""
    skill = _skill()
    row = SkillRow.from_skill(skill)
    assert not hasattr(row, "library")
    back = row.to_skill(
        library=skill.library,
        plugin=skill.plugin,
        root=skill.root,
        category=skill.category,
        tags=skill.tags,
    )
    assert back == skill
    twice = row.to_skill(
        library="other", plugin=skill.plugin, root=skill.root, category=None, tags=frozenset()
    )
    assert twice.address == "other/alpha"
