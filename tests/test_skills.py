"""Unit tests for the catalogue itself, with no MCP layer involved.

SkillIndex is where the scoping rules live, so they are tested directly here
rather than only through the tools that call it.
"""

import pytest

from mcp_school.skills import PackResources, SkillIndex, load_skills


@pytest.fixture
def index(skills_dir):
    return SkillIndex(load_skills(skills_dir))


@pytest.mark.unit
def test_len_and_packs(index):
    assert len(index) == 4
    assert index.packs == ["deepsource", "flatsource"]


@pytest.mark.unit
def test_packs_ignores_the_request_scope(index):
    """/health answers for the pod, not for one pinned client."""
    assert index.packs == ["deepsource", "flatsource"]


@pytest.mark.unit
def test_visible_is_everything_when_unpinned(index):
    assert len(index.visible()) == 4


@pytest.mark.unit
def test_visible_honours_a_pack_or_a_group(index):
    assert {s.name for s in index.visible("flatsource")} == {"alpha", "beta"}
    assert {s.name for s in index.visible("plugin-a")} == {"gamma"}


@pytest.mark.unit
def test_select_cannot_widen_past_the_pin(index):
    """The model's pack argument narrows within the pin, never past it."""
    assert index.select("flatsource", "deepsource") == []
    assert {s.name for s in index.select("flatsource", "flatsource")} == {"alpha", "beta"}


@pytest.mark.unit
def test_selectors_are_scoped_to_the_pin(index):
    """Suggestions must not leak the other packs' names."""
    assert "deepsource" not in index.selectors("flatsource")
    assert "deepsource" in index.selectors()


@pytest.mark.unit
def test_get_hides_out_of_scope_skills(index):
    """Out of scope is indistinguishable from missing, on purpose."""
    assert index.get("gamma") is not None
    assert index.get("gamma", "flatsource") is None
    assert index.get("alpha", "flatsource") is not None


@pytest.mark.unit
def test_qualified_name_resolves(index):
    assert index.get("deepsource/gamma").name == "gamma"


@pytest.mark.unit
def test_empty_index_is_safe(index):
    empty = SkillIndex([])
    assert len(empty) == 0 and empty.packs == [] and empty.get("anything") is None


@pytest.fixture
def resources(skills_dir):
    from mcp_school.skills import PackResources

    return PackResources(skills_dir, load_skills(skills_dir))


@pytest.mark.unit
def test_pack_files_exclude_everything_inside_skills(resources):
    """A skill's own files belong to read_skill, not the pack tool."""
    files = resources.files("deepsource")
    assert set(files) == {"README.md", "shared/guide.md", "shared/nested/schema.json"}
    assert not any("SKILL.md" in f for f in files)


@pytest.mark.unit
def test_pack_with_no_extras_is_empty(resources):
    assert resources.files("flatsource") == []


@pytest.mark.unit
def test_unknown_pack_is_empty(resources):
    assert resources.files("nope") == []


@pytest.mark.unit
def test_read_pack_file(resources):
    assert resources.read("deepsource", "shared/guide.md") == "shared guidance\n"
    assert resources.read("deepsource", "shared/nested/schema.json") == "{}\n"


@pytest.mark.unit
def test_read_refuses_skill_files(resources):
    """Reaching into a skill through the pack tool would bypass skill scoping."""
    assert resources.read("deepsource", "plugin-a/gamma/SKILL.md") is None


@pytest.mark.unit
def test_read_refuses_traversal(resources):
    assert resources.read("deepsource", "../flatsource/alpha/SKILL.md") is None
    assert resources.read("deepsource", "../../etc/passwd") is None


@pytest.mark.unit
def test_listing_never_walks_the_disk_after_startup(skills_dir, monkeypatch):
    """The catalogue is baked into the image; walking it per request froze the
    event loop for ~5s on every resources/list against the real packs."""
    import os
    import pathlib

    from mcp_school.uris import Catalogue

    skills = load_skills(skills_dir)
    resources = PackResources(skills_dir, skills)
    catalogue = Catalogue(SkillIndex(skills), resources)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("listing walked the filesystem")

    monkeypatch.setattr(os, "walk", forbidden)
    monkeypatch.setattr(pathlib.Path, "rglob", forbidden)
    monkeypatch.setattr(pathlib.Path, "iterdir", forbidden)

    assert "shared/guide.md" in resources.files("deepsource")
    assert any(e.uri == "skill://deepsource/_files" for e in catalogue.entries())


@pytest.mark.unit
def test_a_dot_directory_above_the_catalogue_hides_nothing(tmp_path):
    """A skills root under ~/.cache is where the catalogue lives, not a dotfile."""
    from tests.conftest import _build_tree

    root = tmp_path / ".cache" / "skills"
    root.mkdir(parents=True)
    _build_tree(root)
    resources = PackResources(root, load_skills(root))
    assert "shared/guide.md" in resources.files("deepsource")
    assert resources.read("deepsource", "shared/guide.md") == "shared guidance\n"
