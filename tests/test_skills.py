"""Unit tests for the catalogue itself, with no MCP layer involved.

SkillIndex is where the scoping rules live, so they are tested directly here
rather than only through the tools that call it.
"""

import pytest

from kubed.mcp_kb.catalogue.skills import LibraryFiles, Skill, SkillIndex, load_skills
from kubed.mcp_kb.mcp.scope import Scope
from tests.conftest import build_library_files, fake_plugin, load_all_skills


@pytest.fixture
def index(skills_dir):
    return SkillIndex(load_all_skills(skills_dir))


def _tags(*groups):
    """A tag scope: each argument is one comma-group, written as a set."""
    return frozenset(frozenset(group) for group in groups)


@pytest.mark.unit
def test_len_and_libraries(index):
    assert len(index) == 4
    assert index.libraries == ["deepsource", "flatsource"]


@pytest.mark.unit
def test_libraries_ignores_the_request_scope(index):
    """/health answers for the pod, not for one pinned client."""
    assert index.libraries == ["deepsource", "flatsource"]


@pytest.mark.unit
def test_visible_is_everything_when_unpinned(index):
    assert len(index.visible()) == 4


@pytest.mark.unit
def test_visible_honours_a_library_and_a_library_alone(index):
    """A folder name is not a selector: ``deepsource/plugin-a`` names no library."""
    assert {s.name for s in index.visible(Scope("flatsource"))} == {"alpha", "beta"}
    assert index.visible(Scope("deepsource/plugin-a")) == []
    assert index.visible(Scope("plugin-a")) == []


@pytest.mark.unit
def test_visible_honours_the_plugins_category_and_labels(tmp_path):
    """A scope narrows by what the plugin declared: its category, its tags and
    keywords together -- a comma group is all of, separate groups any of."""
    skills = [
        _skill_in(tmp_path, "x", category="ops", tags=frozenset({"lgtm", "oncall"})),
        _skill_in(tmp_path, "y", category="ops", tags=frozenset({"k6"})),
        _skill_in(tmp_path, "z", category="design", tags=frozenset({"lgtm"})),
    ]
    index = SkillIndex(skills)
    names = lambda scope: {s.name for s in index.visible(scope)}  # noqa: E731

    assert names(Scope(categories=frozenset({"ops"}))) == {"x", "y"}
    assert names(Scope(tags=_tags({"lgtm"}))) == {"x", "z"}
    assert names(Scope(tags=_tags({"lgtm", "oncall"}))) == {"x"}
    assert names(Scope(tags=_tags({"oncall"}, {"k6"}))) == {"x", "y"}
    assert names(Scope(categories=frozenset({"ops"}), tags=_tags({"lgtm"}))) == {"x"}
    assert names(Scope("grafana", categories=frozenset({"nope"}))) == set()


def _skill_in(root, name, *, category=None, tags=frozenset()):
    return Skill(
        name=name,
        library="grafana",
        folder="",
        description="",
        path=root / name,
        plugin=f"plugin-{name}",
        root=root,
        category=category,
        tags=tags,
    )


@pytest.mark.unit
def test_get_hides_out_of_scope_skills(index):
    """Out of scope is indistinguishable from missing, on purpose."""
    assert index.get("deepsource/plugin-a/gamma") is not None
    assert index.get("deepsource/plugin-a/gamma", Scope("flatsource")) is None
    assert index.get("flatsource/alpha", Scope("flatsource")) is not None
    assert index.get("flatsource/alpha", Scope(tags=_tags({"deep"}))) is None


@pytest.mark.unit
def test_a_skill_is_found_by_its_full_address_not_its_name(index):
    assert index.get("deepsource/plugin-a/gamma").name == "gamma"
    assert index.get("gamma") is None
    assert index.get("deepsource/gamma") is None


@pytest.mark.unit
def test_empty_index_is_safe(index):
    empty = SkillIndex([])
    assert len(empty) == 0 and empty.libraries == [] and empty.get("anything") is None


@pytest.fixture
def resources(skills_dir):
    return build_library_files(skills_dir)


@pytest.mark.unit
def test_library_files_exclude_everything_inside_skills(resources):
    """A skill's own files belong to read_skill, not the library tool."""
    files = resources.files("deepsource")
    assert set(files) == {"README.md", "shared/guide.md", "shared/nested/schema.json"}
    assert not any("SKILL.md" in f for f in files)


@pytest.mark.unit
def test_library_with_no_extras_is_empty(resources):
    assert resources.files("flatsource") == []


@pytest.mark.unit
def test_unknown_library_is_empty(resources):
    assert resources.files("nope") == []


@pytest.mark.unit
def test_read_library_file(resources):
    assert resources.read("deepsource", "shared/guide.md") == "shared guidance\n"
    assert resources.read("deepsource", "shared/nested/schema.json") == "{}\n"


@pytest.mark.unit
def test_library_files_follow_the_plugins_labels(resources):
    """A file has no row of its own; it is in scope when its plugin is."""
    deep, flat = Scope(tags=_tags({"deep"})), Scope(tags=_tags({"flat"}))
    assert "shared/guide.md" in resources.files("deepsource", deep)
    assert resources.files("deepsource", flat) == []
    assert resources.read("deepsource", "shared/guide.md", deep) == "shared guidance\n"
    assert resources.read("deepsource", "shared/guide.md", flat) is None


@pytest.mark.unit
def test_read_refuses_skill_files(resources):
    """Reaching into a skill through the library tool would bypass skill scoping."""
    assert resources.read("deepsource", "plugin-a/gamma/SKILL.md") is None


@pytest.mark.unit
def test_read_refuses_traversal(resources):
    assert resources.read("deepsource", "../flatsource/alpha/SKILL.md") is None
    assert resources.read("deepsource", "../../etc/passwd") is None


@pytest.mark.unit
def test_listing_never_walks_the_disk_after_startup(skills_dir, monkeypatch):
    """The catalogue is baked into the image; walking it per request froze the
    event loop for ~5s on every resources/list against the real libraries."""
    import os
    import pathlib

    from kubed.mcp_kb.catalogue.uris import Catalogue

    skills = load_all_skills(skills_dir)
    resources = build_library_files(skills_dir)
    catalogue = Catalogue(SkillIndex(skills), resources)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("listing walked the filesystem")

    monkeypatch.setattr(os, "walk", forbidden)
    monkeypatch.setattr(pathlib.Path, "rglob", forbidden)
    monkeypatch.setattr(pathlib.Path, "iterdir", forbidden)

    assert "shared/guide.md" in resources.files("deepsource")
    assert any(e.uri == "skill://deepsource/_files.md" for e in catalogue.entries())


@pytest.mark.unit
def test_a_dot_directory_above_the_catalogue_hides_nothing(tmp_path):
    """A skills root under ~/.cache is where the catalogue lives, not a dotfile."""
    from tests.conftest import _build_tree

    root = tmp_path / ".cache" / "skills"
    root.mkdir(parents=True)
    _build_tree(root)
    resources = build_library_files(root)
    assert "shared/guide.md" in resources.files("deepsource")
    assert resources.read("deepsource", "shared/guide.md") == "shared guidance\n"


@pytest.mark.unit
def test_a_skill_carries_its_plugins_labels_and_nothing_implicit(tmp_path):
    """The plugin's tags and keywords, its category, its id and root -- and no
    library name, plugin name or "skill" smuggled in as a tag."""
    skill_dir = tmp_path / "loki"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: loki\ndescription: d\n---\n")
    plugin = fake_plugin(
        "grafana-skills",
        tmp_path,
        category="observability",
        tags=("upstream",),
        keywords=("logs",),
    )
    skills = load_skills([skill_dir], library="grafana", plugin=plugin, root=tmp_path)

    assert skills[0].tags == frozenset({"upstream", "logs"})
    assert skills[0].category == "observability"
    assert skills[0].plugin == "grafana-skills"
    assert skills[0].root == tmp_path
    assert skills[0].library == "grafana"


@pytest.mark.unit
def test_two_plugins_can_serve_library_files_into_one_library(tmp_path):
    """Several plugins can join one library; files() concatenates their roots."""
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.md").write_text("from a\n")
    (root_b / "b.md").write_text("from b\n")

    resources = LibraryFiles()
    resources.add("lib", root_a, ["a.md"], [], plugin=fake_plugin("a", root_a))
    resources.add("lib", root_b, ["b.md"], [], plugin=fake_plugin("b", root_b))

    assert resources.files("lib") == ["a.md", "b.md"]
    assert resources.read("lib", "b.md") == "from b\n"


@pytest.mark.unit
def test_read_only_serves_the_harvested_list(tmp_path):
    """The list add() was given is the contract, not anything else on disk."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "guide.md").write_text("guidance\n")
    (tmp_path / "README.md").write_text("exists, but a narrower glob skips it\n")
    (tmp_path / ".env").write_text("SECRET=1\n")

    resources = LibraryFiles()
    resources.add(
        "lib", tmp_path, ["shared/guide.md"], [], plugin=fake_plugin("lib", tmp_path)
    )

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

    resources = LibraryFiles()
    resources.add("lib", tmp_path, [], [], plugin=fake_plugin("lib", tmp_path))

    assert resources.read("lib", ".gitkeep") is None


@pytest.mark.unit
def test_add_refuses_a_file_that_resolves_inside_a_skill_dir(tmp_path):
    """The defence in depth ``add()``'s docstring promises: a caller that mis-scoped
    its own file list must not silently publish a skill's own file as a library file."""
    skill_dir = tmp_path / "loki"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: loki\ndescription: d\n---\n")

    resources = LibraryFiles()
    with pytest.raises(ValueError, match="skill directory"):
        resources.add(
            "lib",
            tmp_path,
            ["loki/SKILL.md"],
            [skill_dir],
            plugin=fake_plugin("lib", tmp_path),
        )


@pytest.mark.unit
def test_read_refuses_a_registered_path_whose_target_escapes_the_root(tmp_path):
    """The resolve + is_relative_to guard is defence in depth, kept even though
    ``rel`` is on the harvested list -- a symlink can still point outside."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.md").write_text("secret\n")
    (root / "escape.md").symlink_to(tmp_path / "outside.md")

    resources = LibraryFiles()
    resources.add("lib", root, ["escape.md"], [], plugin=fake_plugin("lib", root))

    assert resources.read("lib", "escape.md") is None
