"""The address space, with no MCP layer involved.

``Catalogue`` is where the grammar and the scoping rules meet, and both halves
of the server are thin projections of it -- so they are tested here directly
rather than only through whichever half happens to be convenient.
"""

import json

import pytest

from kubed.mcp_kb.catalogue.skills import LibraryFiles, SkillIndex, load_skills
from kubed.mcp_kb.catalogue.uris import Catalogue, parse, uri_for
from kubed.mcp_kb.mcp.scope import Scope
from tests.conftest import build_library_files, load_all_skills


@pytest.fixture
def catalogue(skills_dir):
    skills = load_all_skills(skills_dir)
    return Catalogue(SkillIndex(skills), build_library_files(skills_dir))


# -- grammar ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("skill://n8n", ("n8n", "")),
        ("skill://n8n/", ("n8n", "")),
        ("skill://grafana/lgtm/loki/SKILL.md", ("grafana", "lgtm/loki/SKILL.md")),
        ("skill://grafana/lgtm/loki/_manifest", ("grafana", "lgtm/loki/_manifest")),
        ("skill://penpot/shared/a/b.md", ("penpot", "shared/a/b.md")),
    ],
)
def test_parse_splits_head_from_the_rest(uri, expected):
    assert parse(uri) == expected


@pytest.mark.unit
@pytest.mark.parametrize("uri", ["", "skill://", "file://x/y", "grafana/loki", "://x"])
def test_parse_rejects_anything_that_is_not_an_address(uri):
    assert parse(uri) is None


@pytest.mark.unit
def test_parse_does_not_decode_percent_escapes(catalogue):
    """Decoding here would be one more way for %2e%2e to become ``..``."""
    assert parse("skill://p/%2e%2e/x")[1] == "%2e%2e/x"
    assert catalogue.read("skill://deepsource/%2e%2e/README.md") is None


# -- listing ------------------------------------------------------------------


@pytest.mark.unit
def test_entries_are_indexes_not_skills(catalogue):
    """The listing is the cheap layer: libraries and folders, never every skill."""
    uris = [e.uri for e in catalogue.entries()]
    assert uris == [
        "skill://deepsource/_index.md",
        "skill://deepsource/plugin-a/_index.md",
        "skill://deepsource/plugin-b/_index.md",
        "skill://deepsource/_files.md",
        "skill://flatsource/_index.md",
    ]


@pytest.mark.unit
def test_full_listing_adds_every_skill(catalogue):
    """Skill-syncing clients find skills only by scanning for /SKILL.md."""
    uris = [e.uri for e in catalogue.entries(full=True)]
    assert "skill://flatsource/alpha/SKILL.md" in uris
    assert sum(1 for u in uris if u.endswith("/SKILL.md")) == 4


@pytest.mark.unit
def test_entries_carry_the_four_resource_fields(catalogue):
    """The tool mirror publishes these rows verbatim, so the shape is the API."""
    row = catalogue.entries()[0].as_dict()
    assert set(row) == {"uri", "name", "description", "mimeType"}


@pytest.mark.unit
def test_a_library_with_shared_material_advertises_it(catalogue):
    uris = [e.uri for e in catalogue.entries()]
    assert "skill://deepsource/_files.md" in uris
    # flatsource ships only a dotfile, which is not readable material.
    assert "skill://flatsource/_files.md" not in uris


@pytest.mark.unit
def test_entries_honour_the_pin(catalogue):
    uris = [e.uri for e in catalogue.entries(Scope("flatsource"))]
    assert uris == ["skill://flatsource/_index.md"]


@pytest.mark.unit
def test_a_folder_pin_still_names_its_folder(catalogue):
    uris = [e.uri for e in catalogue.entries(Scope("deepsource/plugin-a"))]
    assert "skill://deepsource/plugin-a/_index.md" in uris
    assert "skill://deepsource/plugin-b/_index.md" not in uris


@pytest.mark.unit
def test_a_bare_folder_name_selects_nothing(catalogue):
    """Folder names repeat across libraries; only the path selects one."""
    assert catalogue.entries(Scope("plugin-a")) == []


# -- reading ------------------------------------------------------------------


@pytest.mark.unit
def test_reading_an_index_lists_skills_as_uris(catalogue):
    """An index teaches the grammar for the next call, so it emits addresses."""
    body = catalogue.read("skill://flatsource/_index.md")
    assert "skill://flatsource/alpha/SKILL.md: First skill." in body
    assert "skill://flatsource/beta/SKILL.md: Second skill." in body


@pytest.mark.unit
def test_reading_a_folder_index_excludes_the_sibling_folder(catalogue):
    body = catalogue.read("skill://deepsource/plugin-a/_index.md")
    assert "gamma" in body and "delta" not in body


@pytest.mark.unit
def test_a_skill_root_is_a_directory_not_its_instructions(catalogue):
    """Only `.../SKILL.md` is the instructions; the bare skill path serves nothing."""
    assert catalogue.read("skill://flatsource/alpha") is None
    assert "First skill." in catalogue.read("skill://flatsource/alpha/SKILL.md")


@pytest.mark.unit
def test_manifest_carries_path_size_and_hash(catalogue):
    """fastmcp.utilities.skills raises on a manifest missing any of the three."""
    manifest = json.loads(catalogue.read("skill://flatsource/alpha/_manifest"))
    assert manifest["skill"] == "flatsource/alpha"
    deep = json.loads(catalogue.read("skill://deepsource/plugin-a/gamma/_manifest"))
    assert deep["skill"] == "deepsource/plugin-a/gamma"
    assert set(manifest["files"][0]) == {"path", "size", "hash"}
    assert manifest["files"][0]["hash"].startswith("sha256:")


@pytest.mark.unit
def test_manifest_excludes_a_file_reached_through_a_symlink(catalogue, skills_dir, tmp_path):
    """A symlink out of the skill root must not leak the target's path/size/hash.

    Reading it is already refused by ``_skill_file``'s resolve-then-compare, so
    this is metadata-only, but the manifest should not advertise it either.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("shh\n")
    (skills_dir / "flatsource" / "alpha" / "leak.md").symlink_to(secret)

    manifest = json.loads(catalogue.read("skill://flatsource/alpha/_manifest"))
    assert "leak.md" not in {f["path"] for f in manifest["files"]}


@pytest.mark.unit
def test_manifest_lists_only_the_files_the_harvest_would_keep(catalogue, skills_dir):
    """The manifest is what a client syncing a skill to disk goes shopping from,
    so it advertises what the skill ships and not an editor's swap file."""
    (skills_dir / "flatsource" / "alpha" / ".tmp-half").write_text("not a file\n")

    manifest = json.loads(catalogue.read("skill://flatsource/alpha/_manifest"))

    assert ".tmp-half" not in {f["path"] for f in manifest["files"]}


@pytest.mark.unit
def test_library_files_index_and_one_of_its_files(catalogue):
    index = catalogue.read("skill://deepsource/_files.md")
    assert "skill://deepsource/shared/guide.md" in index
    assert catalogue.read("skill://deepsource/shared/guide.md") == "shared guidance\n"
    assert catalogue.read("skill://deepsource/shared/nested/schema.json") == "{}\n"


@pytest.mark.unit
def test_skill_names_collide_across_libraries_and_both_survive(skills_dir):
    """The library in the URI is what makes this reachable at all.

    SkillsDirectoryProvider keys on the folder name and drops the loser
    entirely; qualifying the address is the fix, so it is pinned by a test.
    """
    for library in ("flatsource", "deepsource"):
        _write_skill(skills_dir, library, "twin", f"from {library}")
    skills = load_all_skills(skills_dir)
    cat = Catalogue(SkillIndex(skills), build_library_files(skills_dir))
    assert "from flatsource" in cat.read("skill://flatsource/twin/SKILL.md")
    assert "from deepsource" in cat.read("skill://deepsource/twin/SKILL.md")


def _write_skill(root, library, name, description):
    path = root / library / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n"
    )


# -- absence and scope --------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    [
        "skill://nope",
        "skill://nope/_index.md",
        "skill://flatsource/nope",
        "skill://flatsource/nope/SKILL.md",
        "skill://flatsource/_files.md",
        "skill://deepsource/plugin-c/_index.md",
        "skill://flatsource/alpha/nope.md",
        "skill://deepsource/shared/nope.md",
        "not-a-uri",
    ],
)
def test_absent_reads_are_none_not_errors(catalogue, uri):
    assert catalogue.read(uri) is None


@pytest.mark.unit
def test_traversal_cannot_walk_out_of_a_skill(catalogue):
    assert catalogue.read("skill://deepsource/plugin-a/gamma/../../README.md") is None
    assert (
        catalogue.read("skill://deepsource/plugin-a/gamma/../../../etc/passwd")
        is None
    )


@pytest.mark.unit
def test_a_skill_file_is_not_reachable_as_a_library_file(catalogue):
    """The two spaces must not overlap, or skill scoping could be bypassed."""
    assert catalogue.read("skill://deepsource/plugin-a/gamma/SKILL.md") is not None
    pinned = Scope("deepsource/plugin-b")
    assert catalogue.read("skill://deepsource/plugin-a/gamma/SKILL.md", pinned) is None


@pytest.mark.unit
def test_the_pin_hides_another_library_entirely(catalogue):
    """Out of scope is indistinguishable from absent, on purpose."""
    gamma = "skill://deepsource/plugin-a/gamma/SKILL.md"
    flat = Scope("flatsource")
    assert catalogue.read(gamma) is not None
    assert catalogue.read(gamma, scope=flat) is None
    assert catalogue.read("skill://deepsource/_index.md", scope=flat) is None
    assert catalogue.read("skill://deepsource/plugin-a/_index.md", scope=flat) is None


@pytest.mark.unit
def test_a_folder_pin_cannot_read_a_sibling_folder(catalogue):
    """Pinning to one folder must not widen to the whole library."""
    pinned = Scope("deepsource/plugin-a")
    assert catalogue.read("skill://deepsource/plugin-a/gamma/SKILL.md", pinned)
    assert catalogue.read("skill://deepsource/plugin-b/delta/SKILL.md", pinned) is None
    assert catalogue.read("skill://deepsource/plugin-b/_index.md", pinned) is None
    assert "delta" not in catalogue.read("skill://deepsource/_index.md", pinned)


@pytest.mark.unit
def test_a_folder_pin_still_reaches_its_sources_shared_material(catalogue):
    """Shared files come from the source whose skills cite them."""
    body = catalogue.read(
        "skill://deepsource/shared/guide.md", scope=Scope("deepsource/plugin-a")
    )
    assert body == "shared guidance\n"


@pytest.mark.unit
def test_the_pin_blocks_shared_material_of_another_library(catalogue):
    assert (
        catalogue.read("skill://deepsource/shared/guide.md", scope=Scope("flatsource"))
        is None
    )
    assert (
        catalogue.read("skill://deepsource/_files.md", scope=Scope("flatsource"))
        is None
    )


@pytest.mark.unit
def test_uri_for_is_the_address_the_catalogue_answers(catalogue, skills_dir):
    skill = next(s for s in load_all_skills(skills_dir) if s.name == "gamma")
    assert uri_for(skill) == "skill://deepsource/plugin-a/gamma/SKILL.md"
    assert catalogue.read(uri_for(skill)) is not None
    assert catalogue.read(uri_for(skill, "_manifest")) is not None


@pytest.mark.unit
def test_mime_types_follow_the_content(catalogue):
    assert catalogue.mime("skill://deepsource/_index.md") == "text/markdown"
    assert catalogue.mime("skill://flatsource/alpha/_manifest") == "application/json"
    assert (
        catalogue.mime("skill://deepsource/shared/nested/schema.json")
        == "application/json"
    )


# -- tags -----------------------------------------------------------------------


@pytest.mark.unit
def test_an_index_row_is_tagged_with_its_library(tmp_path):
    """A library index row is tagged with its own library and "index", sorted."""
    skill_dir = tmp_path / "loki"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: loki\ndescription: d\n---\n")
    skills = load_skills(
        [skill_dir], library="grafana", source="grafana", root=tmp_path
    )
    resources = LibraryFiles()
    cat = Catalogue(SkillIndex(skills), resources)
    assert cat.entries()[0].tags == ("grafana", "index")


@pytest.mark.unit
def test_a_full_row_carries_the_skills_own_tags(catalogue, skills_dir):
    """The full listing is a per-skill row, so it carries that skill's tags."""
    skill = next(s for s in load_all_skills(skills_dir) if s.name == "gamma")
    row = next(
        e for e in catalogue.entries(full=True) if e.uri == uri_for(skill)
    )
    assert row.tags == tuple(sorted(skill.tags))
