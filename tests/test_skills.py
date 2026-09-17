"""Unit tests for the catalogue itself, with no MCP layer involved.

SkillIndex is where the scoping rules live, so they are tested directly here
rather than only through the tools that call it.
"""

import pytest

from kubed.mcp_kb.catalogue.skills import PackResources, SkillIndex, load_skills
from kubed.mcp_kb.mcp.scope import Scope
from tests.conftest import build_pack_resources, load_all_skills


@pytest.fixture
def index(skills_dir):
    return SkillIndex(load_all_skills(skills_dir))


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
    assert {s.name for s in index.visible(Scope("flatsource"))} == {"alpha", "beta"}
    assert {s.name for s in index.visible(Scope("plugin-a"))} == {"gamma"}


@pytest.mark.unit
def test_select_cannot_widen_past_the_pin(index):
    """The model's pack argument narrows within the pin, never past it."""
    assert index.select(Scope("flatsource"), "deepsource") == []
    assert {s.name for s in index.select(Scope("flatsource"), "flatsource")} == {
        "alpha",
        "beta",
    }


@pytest.mark.unit
def test_selectors_are_scoped_to_the_pin(index):
    """Suggestions must not leak the other packs' names."""
    assert "deepsource" not in index.selectors(Scope("flatsource"))
    assert "deepsource" in index.selectors()


@pytest.mark.unit
def test_get_hides_out_of_scope_skills(index):
    """Out of scope is indistinguishable from missing, on purpose."""
    assert index.get("gamma") is not None
    assert index.get("gamma", Scope("flatsource")) is None
    assert index.get("alpha", Scope("flatsource")) is not None


@pytest.mark.unit
def test_qualified_name_resolves(index):
    assert index.get("deepsource/gamma").name == "gamma"


@pytest.mark.unit
def test_empty_index_is_safe(index):
    empty = SkillIndex([])
    assert len(empty) == 0 and empty.packs == [] and empty.get("anything") is None


@pytest.fixture
def resources(skills_dir):
    return build_pack_resources(skills_dir)


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

    from kubed.mcp_kb.catalogue.uris import Catalogue

    skills = load_all_skills(skills_dir)
    resources = build_pack_resources(skills_dir)
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
    resources = build_pack_resources(root)
    assert "shared/guide.md" in resources.files("deepsource")
    assert resources.read("deepsource", "shared/guide.md") == "shared guidance\n"


@pytest.mark.unit
def test_a_skill_carries_its_library_source_and_kind_as_tags(tmp_path):
    """The pack is the library; source and "skill" ride along with any extras."""
    skill_dir = tmp_path / "loki"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: loki\ndescription: d\n---\n")
    skills = load_skills(
        [skill_dir],
        pack="grafana",
        source="grafana-skills",
        root=tmp_path,
        tags=["upstream"],
    )
    assert skills[0].tags == frozenset(
        {"grafana", "grafana-skills", "skill", "upstream"}
    )


@pytest.mark.unit
def test_two_sources_can_serve_pack_files_into_one_library(tmp_path):
    """Several sources can join one library; files() concatenates their roots."""
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.md").write_text("from a\n")
    (root_b / "b.md").write_text("from b\n")

    resources = PackResources()
    resources.add("lib", root_a, ["a.md"], [])
    resources.add("lib", root_b, ["b.md"], [])

    assert resources.files("lib") == ["a.md", "b.md"]
    assert resources.read("lib", "b.md") == "from b\n"


@pytest.mark.unit
def test_read_only_serves_the_harvested_list(tmp_path):
    """The list add() was given is the contract, not anything else on disk."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "guide.md").write_text("guidance\n")
    (tmp_path / "README.md").write_text("exists, but a narrower include skips it\n")
    (tmp_path / ".env").write_text("SECRET=1\n")

    resources = PackResources()
    resources.add("lib", tmp_path, ["shared/guide.md"], [])

    assert resources.read("lib", "shared/guide.md") == "guidance\n"
    # Both exist on disk and are neither dotfiles-inside-a-skill nor traversal
    # attempts -- only their absence from the harvested list refuses them.
    assert resources.read("lib", "README.md") is None
    assert resources.read("lib", ".env") is None


@pytest.mark.unit
def test_read_refuses_an_unregistered_file_that_exists_on_disk(tmp_path):
    """Pins the allow-list: an existing but never-harvested file stays unreadable,
    the same way grafana's real `.gitkeep` placeholders do."""
    (tmp_path / ".gitkeep").write_text("")

    resources = PackResources()
    resources.add("lib", tmp_path, [], [])

    assert resources.read("lib", ".gitkeep") is None


@pytest.mark.unit
def test_add_refuses_a_file_that_resolves_inside_a_skill_dir(tmp_path):
    """The defence in depth ``add()``'s docstring promises: a caller that mis-scoped
    its own file list must not silently publish a skill's own file as a pack file."""
    skill_dir = tmp_path / "loki"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: loki\ndescription: d\n---\n")

    resources = PackResources()
    with pytest.raises(ValueError, match="skill directory"):
        resources.add("lib", tmp_path, ["loki/SKILL.md"], [skill_dir])


@pytest.mark.unit
def test_load_skills_drops_an_empty_source_tag(tmp_path):
    """``source=""`` must not put an empty-string tag in the set."""
    skill_dir = tmp_path / "loki"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: loki\ndescription: d\n---\n")

    skills = load_skills([skill_dir], pack="grafana", source="", root=tmp_path)

    assert "" not in skills[0].tags
    assert skills[0].tags == frozenset({"grafana", "skill"})


@pytest.mark.unit
def test_read_refuses_a_registered_path_whose_target_escapes_the_root(tmp_path):
    """The resolve + is_relative_to guard is defence in depth, kept even though
    ``rel`` is on the harvested list -- a symlink can still point outside."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.md").write_text("secret\n")
    (root / "escape.md").symlink_to(tmp_path / "outside.md")

    resources = PackResources()
    resources.add("lib", root, ["escape.md"], [])

    assert resources.read("lib", "escape.md") is None
