"""Addresses, fetches, plugins, manifests and selectors: the resolution layer."""

import json

import pytest

from kubed.mcp_kb.config import Config, PluginConfig, Selector
from kubed.mcp_kb.plugins import (
    Fetch,
    Globs,
    Plugin,
    declared_plugins,
    fetch_for,
    marketplace_fetches,
    plugin_from_config,
)
from kubed.mcp_kb.plugins.address import parse_address
from kubed.mcp_kb.plugins.manifest import Manifest, read_manifest
from kubed.mcp_kb.plugins.select import matches, parse_groups, select

CONFIG = {
    "sources": [
        {"name": "github", "url": "git+https://github.com", "refresh": "1h"},
        {
            "name": "nextcloud",
            "url": "webdav+http://cloud.example/remote.php/dav/files",
            "cache": "live",
            "auth": {"username": "me", "password": {"env": "NEXTCLOUD_PASSWORD"}},
        },
        {"name": "local", "url": "file:///srv/library"},
    ]
}


def config(**extra) -> Config:
    return Config.model_validate({**CONFIG, **extra})


# -- the address grammar -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "github://grafana/skills",
        "github://grafana/skills//skills/loki",
        "github://grafana/skills?ref=main",
        "github://grafana/skills//sub?ref=51d33e7",
        "nextcloud://mcp-kb/ai",
        "file:///srv/prompts/grafana",
        "file:///srv/prompts//grafana",
    ],
)
def test_a_canonical_address_round_trips_both_ways(text):
    """The string is the config's spelling, so it has to survive a parse."""
    address = parse_address(text)
    assert str(address) == text
    assert parse_address(str(address)) == address


def test_the_first_double_slash_is_the_subdir_split():
    address = parse_address("github://owner/repo//docs/skills")
    assert (address.path, address.subdir) == ("owner/repo", "docs/skills")


def test_a_subdir_may_hold_slashes_but_the_path_may_not_hold_a_double_one():
    assert parse_address("gh://a/b//c/d/e").subdir == "c/d/e"


def test_the_key_is_the_fetch_identity_and_drops_the_subdir():
    """Two plugins in one repository are one clone; the subdir only says where."""
    one = parse_address("github://grafana/skills//skills/loki?ref=main")
    two = parse_address("github://grafana/skills//skills/tempo?ref=main")

    assert one.key == two.key == "github://grafana/skills?ref=main"
    assert parse_address("github://grafana/skills").key == "github://grafana/skills"


def test_an_unknown_query_key_is_refused_and_named():
    with pytest.raises(ValueError, match="rev"):
        parse_address("github://o/r?rev=main")


def test_a_ref_with_no_value_is_refused():
    with pytest.raises(ValueError, match="ref"):
        parse_address("github://o/r?ref=")


@pytest.mark.parametrize(
    "text", ["github://o/../r", "github://o/r//../etc", "file:///srv/../etc"]
)
def test_a_parent_traversal_is_refused_anywhere_in_an_address(text):
    with pytest.raises(ValueError, match=r"\.\."):
        parse_address(text)


@pytest.mark.parametrize("text", ["github://", "github:///", "file:///"])
def test_an_address_with_no_path_is_refused(text):
    with pytest.raises(ValueError, match="path"):
        parse_address(text)


@pytest.mark.parametrize("text", ["grafana/skills", "github:/o/r", "HTTP://o/r"])
def test_something_that_is_not_an_address_says_what_one_looks_like(text):
    with pytest.raises(ValueError, match="source"):
        parse_address(text)


def test_a_file_address_is_absolute_and_a_source_address_is_not():
    with pytest.raises(ValueError, match="absolute"):
        parse_address("file://relative/path")
    with pytest.raises(ValueError, match="leading"):
        parse_address("github:///owner/repo")


def test_trailing_slashes_are_not_part_of_the_path():
    assert parse_address("github://owner/repo/").path == "owner/repo"
    assert parse_address("github://owner/repo//sub/").subdir == "sub"


# -- fetches -------------------------------------------------------------------


def test_a_builtin_file_address_fetches_the_path_itself():
    fetch = fetch_for(config(), parse_address("file:///srv/prompts"))

    assert fetch == Fetch(key="file:///srv/prompts", backend="file", url="/srv/prompts")
    assert fetch.live is False


def test_a_git_address_becomes_a_clone_url_with_no_git_suffix_added():
    fetch = fetch_for(config(), parse_address("github://grafana/skills?ref=abc123"))

    assert fetch.backend == "git"
    assert fetch.url == "https://github.com/grafana/skills"
    assert fetch.ref == "abc123"
    assert fetch.refresh_seconds == 3600


def test_a_webdav_address_becomes_a_folder_url_and_carries_the_login():
    here = config()
    fetch = fetch_for(here, parse_address("nextcloud://mcp-kb/ai"))

    assert fetch.backend == "webdav"
    assert fetch.url == "http://cloud.example/remote.php/dav/files/mcp-kb/ai"
    assert fetch.auth is here.source("nextcloud").auth
    assert fetch.live is True


def test_a_file_source_joins_its_directory_to_the_address_path():
    fetch = fetch_for(config(), parse_address("local://prompts/grafana"))

    assert (fetch.backend, fetch.url) == ("file", "/srv/library/prompts/grafana")


def test_two_addresses_with_one_key_are_one_fetch():
    """One clone for a marketplace's seven entries -- equality is what dedups it."""
    here = config()
    loki = fetch_for(here, parse_address("github://grafana/skills//a?ref=v1"))
    tempo = fetch_for(here, parse_address("github://grafana/skills//b?ref=v1"))

    assert loki == tempo
    assert len({loki, tempo}) == 1


def test_the_slug_is_a_short_stable_digest_of_the_key():
    """It names a cache directory, so it must be filesystem-safe and per-ref."""
    here = config()
    once = fetch_for(here, parse_address("github://grafana/skills?ref=v1"))
    again = fetch_for(here, parse_address("github://grafana/skills//sub?ref=v1"))
    other = fetch_for(here, parse_address("github://grafana/skills?ref=v2"))

    assert once.slug == again.slug != other.slug
    assert len(once.slug) == 8
    assert once.slug.isalnum() and once.slug.islower()


# -- plugins -------------------------------------------------------------------


def test_a_declared_plugin_keeps_its_name_as_its_id_and_its_subdir():
    plugin = plugin_from_config(
        config(),
        PluginConfig(
            name="team-notes",
            description="Runbooks.",
            category="engineering",
            tags=["runbooks", "oncall"],
            version="1.2.3",
            source="github://my-team/my-repo//foo?ref=main",
            skills=["runbooks/*"],
        ),
    )

    assert plugin.id == plugin.name == "team-notes"
    assert plugin.subdir == "foo"
    assert plugin.category == "engineering"
    assert plugin.version == "1.2.3"
    assert plugin.marketplace is None
    assert plugin.globs == Globs(skills=("runbooks/*",))
    assert plugin.dialect == "auto"


def test_a_kind_nobody_configured_stays_none_so_the_manifest_can_speak():
    plugin = plugin_from_config(
        config(), PluginConfig(name="p", source="github://o/r", prompts=[])
    )

    assert plugin.globs == Globs(skills=None, prompts=(), files=None)


def test_labels_are_the_tags_and_the_keywords_together():
    """A marketplace entry carries tags, a plugin.json carries keywords, and a
    selector should not have to know which one a plugin was published with."""
    plugin = plugin_from_config(
        config(),
        PluginConfig(
            name="p", source="github://o/r", tags=["loki"], keywords=["tdd", "loki"]
        ),
    )

    assert plugin.labels == frozenset({"loki", "tdd"})


def test_declared_plugins_resolves_every_one_and_shares_their_fetch():
    here = config(
        plugins=[
            {"name": "a", "source": "github://grafana/skills//a?ref=v1"},
            {"name": "b", "source": "github://grafana/skills//b?ref=v1"},
            {"name": "c", "source": "file:///srv/x"},
        ]
    )
    plugins = declared_plugins(here)

    assert [p.id for p in plugins] == ["a", "b", "c"]
    assert plugins[0].fetch == plugins[1].fetch
    assert plugins[2].fetch.backend == "file"


def test_marketplace_fetches_are_the_libraries_that_have_a_catalog():
    """D1 reads each of these to turn a marketplace into plugins; a library of
    named plugins has no catalog to read and must not appear."""
    here = config(
        plugins=[{"name": "mine", "source": "github://o/r"}],
        libraries=[
            {"name": "grafana", "source": "github://grafana/skills?ref=v1"},
            {"name": "kubed", "plugins": ["mine"]},
        ],
    )
    found = marketplace_fetches(here)

    assert list(found) == ["grafana"]
    library, fetch, address = found["grafana"]
    assert library is here.library("grafana")
    assert fetch == fetch_for(here, parse_address("github://grafana/skills?ref=v1"))
    assert address.path == "grafana/skills"


# -- manifests -----------------------------------------------------------------


def manifest(tmp_path, data, *, where=".claude-plugin/plugin.json"):
    path = tmp_path / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return tmp_path


def test_no_manifest_is_no_manifest(tmp_path):
    assert read_manifest(tmp_path) is None


def test_penpots_manifest_shape_is_read_as_published(tmp_path):
    """Verbatim from penpot/penpot-ai-kit's `.claude-plugin/plugin.json`."""
    root = manifest(
        tmp_path,
        {
            "name": "penpot-ai-kit",
            "description": "Design with Penpot from Claude.",
            "version": "0.1.0",
            "keywords": ["penpot", "design"],
            "commands": ["./prompts/design-brief.md", "./prompts/deck-brief.md"],
        },
    )

    assert read_manifest(root) == Manifest(
        name="penpot-ai-kit",
        description="Design with Penpot from Claude.",
        version="0.1.0",
        keywords=("penpot", "design"),
        skills=None,
        commands=("prompts/design-brief.md", "prompts/deck-brief.md"),
    )


def test_a_bare_plugin_json_is_read_too(tmp_path):
    root = manifest(tmp_path, {"name": "p", "keywords": ["x"]}, where="plugin.json")
    assert read_manifest(root).keywords == ("x",)


def test_superpowers_manifest_shape_carries_keywords_and_nothing_else(tmp_path):
    """obra/superpowers: no category, no tags, no component lists -- keywords only."""
    root = manifest(
        tmp_path, {"name": "superpowers", "keywords": ["skills", "tdd", "workflow"]}
    )
    read = read_manifest(root)

    assert read.keywords == ("skills", "tdd", "workflow")
    assert read.skills is None and read.commands is None


def test_keys_this_server_has_no_use_for_are_ignored_not_refused(tmp_path):
    """n8n-io/skills ships `userConfig` and `mcpServers`; neither is ours."""
    root = manifest(
        tmp_path,
        {"name": "n8n", "userConfig": {"apiKey": {}}, "mcpServers": {"n8n": {}}},
    )
    assert read_manifest(root).name == "n8n"


def test_a_single_string_is_as_good_as_a_list(tmp_path):
    root = manifest(tmp_path, {"skills": "./skills/one", "commands": "./prompts"})
    read = read_manifest(root)

    assert read.skills == ("skills/one",)
    assert read.commands == ("prompts/*.md",)


def test_a_command_directory_becomes_a_markdown_glob(tmp_path):
    """Claude takes a directory of commands; a harvest takes patterns."""
    root = manifest(tmp_path, {"commands": ["commands", "./one.md", "sub/*.md"]})

    assert read_manifest(root).commands == ("commands/*.md", "one.md", "sub/*.md")


def test_a_component_list_of_the_wrong_type_names_the_field(tmp_path):
    root = manifest(tmp_path, {"skills": {"loki": True}})
    with pytest.raises(ValueError, match="skills"):
        read_manifest(root)


def test_a_path_escaping_the_plugin_root_is_dropped_not_raised(tmp_path):
    """One bad path in a published manifest must not cost the whole plugin."""
    root = manifest(tmp_path, {"skills": ["../elsewhere", "/etc", "skills/ok"]})

    assert read_manifest(root).skills == ("skills/ok",)


def test_an_unparsable_manifest_is_an_error(tmp_path):
    (tmp_path / "plugin.json").write_text("{not json")
    with pytest.raises(ValueError, match=r"plugin\.json"):
        read_manifest(tmp_path)


# -- selectors -----------------------------------------------------------------


def test_parse_groups_reads_a_comma_as_all_of_and_an_item_as_any_of():
    assert parse_groups(["runbooks,oncall", "lgtm"]) == frozenset(
        {frozenset({"runbooks", "oncall"}), frozenset({"lgtm"})}
    )


def test_parse_groups_drops_what_says_nothing():
    assert parse_groups(["", "  ", ",,", "a, b"]) == frozenset({frozenset({"a", "b"})})


def plugin(category=None, labels=()) -> Plugin:
    return Plugin(
        id="p",
        name="p",
        description="",
        category=category,
        tags=tuple(labels),
        keywords=(),
        version=None,
        fetch=Fetch(key="file:///x", backend="file", url="/x"),
        subdir="",
        globs=Globs(),
    )


def test_an_empty_selector_constrains_nothing():
    assert matches(plugin(), frozenset(), frozenset())
    assert matches(plugin("design", ["a"]), frozenset(), frozenset())


def test_categories_are_any_of():
    categories = frozenset({"design", "engineering"})

    assert matches(plugin("engineering"), categories, frozenset())
    assert not matches(plugin("homelab"), categories, frozenset())
    assert not matches(plugin(None), categories, frozenset())


def test_a_tag_group_is_all_of_and_the_groups_are_any_of():
    groups = parse_groups(["runbooks,oncall", "lgtm"])

    assert matches(plugin(labels=["runbooks", "oncall", "x"]), frozenset(), groups)
    assert matches(plugin(labels=["lgtm"]), frozenset(), groups)
    assert not matches(plugin(labels=["runbooks"]), frozenset(), groups)
    assert not matches(plugin(labels=["other"]), frozenset(), groups)


def test_a_category_and_a_tag_narrow_each_other():
    categories, groups = frozenset({"observability"}), parse_groups(["loki"])

    assert matches(plugin("observability", ["loki"]), categories, groups)
    assert not matches(plugin("observability", ["tempo"]), categories, groups)
    assert not matches(plugin("design", ["loki"]), categories, groups)


def test_select_keeps_the_plugins_a_selector_admits_in_order():
    here = config(
        plugins=[
            {"name": "a", "source": "github://o/r", "category": "engineering",
             "tags": ["runbooks", "oncall"]},
            {"name": "b", "source": "github://o/r2", "category": "engineering"},
            {"name": "c", "source": "github://o/r3", "category": "design",
             "keywords": ["runbooks", "oncall"]},
        ]
    )
    selector = Selector(categories=["engineering", "design"], tags=["runbooks,oncall"])

    assert [p.id for p in select(declared_plugins(here), selector)] == ["a", "c"]
