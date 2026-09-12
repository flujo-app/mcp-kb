"""The address space, with no MCP layer involved.

``Catalogue`` is where the grammar and the scoping rules meet, and both halves
of the server are thin projections of it -- so they are tested here directly
rather than only through whichever half happens to be convenient.
"""

import json

import pytest

from kubed.skills_mcp.skills import PackResources, SkillIndex, load_skills
from kubed.skills_mcp.uris import Catalogue, parse, uri_for


@pytest.fixture
def catalogue(skills_dir):
    skills = load_skills(skills_dir)
    return Catalogue(SkillIndex(skills), PackResources(skills_dir, skills))


# -- grammar ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("skill://n8n", ("n8n", "")),
        ("skill://n8n/", ("n8n", "")),
        ("skill://grafana/loki", ("grafana", "loki")),
        ("skill://grafana/loki/_manifest", ("grafana", "loki/_manifest")),
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
    """The listing is the cheap layer: packs and groups, never every skill."""
    uris = [e.uri for e in catalogue.entries()]
    assert "skill://flatsource" in uris
    assert "skill://deepsource" in uris
    assert "skill://plugin-a" in uris
    assert not any(u.endswith("/alpha") or u.endswith("SKILL.md") for u in uris)


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
def test_a_pack_with_shared_material_advertises_it(catalogue):
    uris = [e.uri for e in catalogue.entries()]
    assert "skill://deepsource/_files" in uris
    # flatsource ships only a dotfile, which is not readable material.
    assert "skill://flatsource/_files" not in uris


@pytest.mark.unit
def test_entries_honour_the_pin(catalogue):
    uris = [e.uri for e in catalogue.entries("flatsource")]
    assert uris == ["skill://flatsource"]


@pytest.mark.unit
def test_a_group_pin_still_names_its_group(catalogue):
    uris = [e.uri for e in catalogue.entries("plugin-a")]
    assert "skill://plugin-a" in uris
    assert "skill://plugin-b" not in uris


# -- reading ------------------------------------------------------------------


@pytest.mark.unit
def test_reading_an_index_lists_skills_as_uris(catalogue):
    """An index teaches the grammar for the next call, so it emits addresses."""
    body = catalogue.read("skill://flatsource")
    assert "skill://flatsource/alpha: First skill." in body
    assert "skill://flatsource/beta: Second skill." in body


@pytest.mark.unit
def test_reading_a_group_index_excludes_the_sibling_group(catalogue):
    body = catalogue.read("skill://plugin-a")
    assert "gamma" in body and "delta" not in body


@pytest.mark.unit
def test_a_bare_skill_uri_is_its_instructions(catalogue):
    """`skill://pack/name` and `.../SKILL.md` are the same thing said twice."""
    short = catalogue.read("skill://flatsource/alpha")
    explicit = catalogue.read("skill://flatsource/alpha/SKILL.md")
    assert short == explicit
    assert "First skill." in short


@pytest.mark.unit
def test_manifest_carries_path_size_and_hash(catalogue):
    """fastmcp.utilities.skills raises on a manifest missing any of the three."""
    manifest = json.loads(catalogue.read("skill://flatsource/alpha/_manifest"))
    assert manifest["skill"] == "flatsource/alpha"
    assert set(manifest["files"][0]) == {"path", "size", "hash"}
    assert manifest["files"][0]["hash"].startswith("sha256:")


@pytest.mark.unit
def test_pack_files_index_and_one_of_its_files(catalogue):
    index = catalogue.read("skill://deepsource/_files")
    assert "skill://deepsource/shared/guide.md" in index
    assert catalogue.read("skill://deepsource/shared/guide.md") == "shared guidance\n"
    assert catalogue.read("skill://deepsource/shared/nested/schema.json") == "{}\n"


@pytest.mark.unit
def test_skill_names_collide_across_packs_and_both_survive(skills_dir):
    """The pack in the URI is what makes this reachable at all.

    SkillsDirectoryProvider keys on the folder name and drops the loser
    entirely; qualifying the address is the fix, so it is pinned by a test.
    """
    for pack in ("flatsource", "deepsource"):
        _write_skill(skills_dir, pack, "twin", f"from {pack}")
    skills = load_skills(skills_dir)
    cat = Catalogue(SkillIndex(skills), PackResources(skills_dir, skills))
    assert "from flatsource" in cat.read("skill://flatsource/twin")
    assert "from deepsource" in cat.read("skill://deepsource/twin")


def _write_skill(root, pack, name, description):
    path = root / pack / name
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
        "skill://flatsource/nope",
        "skill://flatsource/alpha/nope.md",
        "skill://deepsource/shared/nope.md",
        "not-a-uri",
    ],
)
def test_absent_reads_are_none_not_errors(catalogue, uri):
    assert catalogue.read(uri) is None


@pytest.mark.unit
def test_traversal_cannot_walk_out_of_a_skill(catalogue):
    assert catalogue.read("skill://deepsource/gamma/../../README.md") is None
    assert catalogue.read("skill://deepsource/gamma/../../../etc/passwd") is None


@pytest.mark.unit
def test_a_skill_file_is_not_reachable_as_a_pack_file(catalogue):
    """The two spaces must not overlap, or skill scoping could be bypassed."""
    assert catalogue.read("skill://deepsource/plugin-a/gamma/SKILL.md") is None


@pytest.mark.unit
def test_the_pin_hides_another_pack_entirely(catalogue):
    """Out of scope is indistinguishable from absent, on purpose."""
    assert catalogue.read("skill://deepsource/gamma") is not None
    assert catalogue.read("skill://deepsource/gamma", pinned="flatsource") is None
    assert catalogue.read("skill://deepsource", pinned="flatsource") is None


@pytest.mark.unit
def test_a_group_pin_cannot_read_a_sibling_group(catalogue):
    """Pinning to one group must not widen to the whole pack."""
    assert catalogue.read("skill://deepsource/gamma", pinned="plugin-a") is not None
    assert catalogue.read("skill://deepsource/delta", pinned="plugin-a") is None


@pytest.mark.unit
def test_a_group_pin_still_reaches_its_packs_shared_material(catalogue):
    """Shared files belong to the pack, and are what its skills cite."""
    body = catalogue.read("skill://deepsource/shared/guide.md", pinned="plugin-a")
    assert body == "shared guidance\n"


@pytest.mark.unit
def test_the_pin_blocks_shared_material_of_another_pack(catalogue):
    assert catalogue.read("skill://deepsource/shared/guide.md", pinned="flatsource") is None
    assert catalogue.read("skill://deepsource/_files", pinned="flatsource") is None


@pytest.mark.unit
def test_uri_for_is_the_address_the_catalogue_answers(catalogue, skills_dir):
    skill = next(s for s in load_skills(skills_dir) if s.name == "gamma")
    assert uri_for(skill) == "skill://deepsource/gamma"
    assert catalogue.read(uri_for(skill)) is not None
    assert catalogue.read(uri_for(skill, "_manifest")) is not None


@pytest.mark.unit
def test_mime_types_follow_the_content(catalogue):
    assert catalogue.mime("skill://deepsource") == "text/markdown"
    assert catalogue.mime("skill://flatsource/alpha/_manifest") == "application/json"
    assert catalogue.mime("skill://deepsource/shared/nested/schema.json") == "application/json"
