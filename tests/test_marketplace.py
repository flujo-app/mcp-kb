"""Reading a `marketplace.json`: every entry form, and everything skipped.

The entry shapes here are copied from the marketplaces this server actually
pulls -- grafana/skills, penpot/penpot-ai-kit, obra/superpowers, n8n-io/skills
-- because a catalog is somebody else's file and a hand-invented one proves
only that the reader agrees with the test.
"""

import json

import pytest

from kubed.mcp_kb.config import Config, LibraryConfig
from kubed.mcp_kb.plugins import Globs, fetch_for
from kubed.mcp_kb.plugins.address import parse_address
from kubed.mcp_kb.plugins.marketplace import (
    MARKETPLACE_FILES,
    find_marketplace,
    read_marketplace,
)

SOURCES = {
    "sources": [
        {"name": "github", "url": "git+https://github.com"},
        {"name": "gitlab", "url": "git+https://gitlab.com"},
    ]
}

# grafana/skills' .claude-plugin/marketplace.json, three entries verbatim.
GRAFANA = {
    "name": "grafana-skills",
    "owner": {"name": "Grafana Labs"},
    "plugins": [
        {
            "name": "grafana-lgtm",
            "source": "./",
            "description": "Query Loki, Tempo, Mimir and Pyroscope.",
            "version": "0.1.0",
            "category": "grafana",
            "tags": ["loki", "tempo", "mimir", "pyroscope"],
            "skills": [
                "./skills/grafana-lgtm/loki",
                "./skills/grafana-lgtm/tempo",
                "./skills/grafana-lgtm/mimir",
            ],
        },
        {
            "name": "grafana-dashboards",
            "source": "./",
            "description": "Build dashboards.",
            "version": "0.1.0",
            "category": "grafana",
            "tags": ["dashboards"],
            "skills": ["./skills/grafana-dashboards/panels"],
        },
        {
            "name": "grafana-alerting",
            "source": "./",
            "description": "Alert rules and notifications.",
            "version": "0.1.0",
            "category": "grafana",
            "tags": ["alerting"],
            "skills": ["./skills/grafana-alerting/rules"],
        },
    ],
}

# penpot/penpot-ai-kit: one entry, and no component lists on it at all --
# its .claude-plugin/plugin.json is what names the commands.
PENPOT = {
    "name": "penpot-ai-kit",
    "plugins": [
        {
            "name": "penpot-ai-kit",
            "source": "./",
            "description": "Design with Penpot from Claude.",
            "category": "design",
            "tags": ["design", "penpot", "mcp"],
        }
    ],
}


def config(**extra) -> Config:
    return Config.model_validate({**SOURCES, **extra})


def marketplace(tmp_path, data, *, where=MARKETPLACE_FILES[0], subdir=""):
    root = tmp_path / subdir if subdir else tmp_path
    path = root / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return tmp_path


def read(tmp_path, data, *, source="github://grafana/skills?ref=abc", **kwargs):
    """Read a catalog written into ``tmp_path`` for a library at ``source``."""
    here = kwargs.pop("config", config())
    library = LibraryConfig(name="grafana", source=source)
    address = parse_address(source)
    root = tmp_path / address.subdir if address.subdir else tmp_path
    marketplace(tmp_path, data, subdir=address.subdir, **kwargs)
    return read_marketplace(
        root,
        library=library,
        fetch=fetch_for(here, address),
        address=address,
        config=here,
    )


def entry(**fields) -> dict:
    return {"plugins": [{"name": "p", "source": "./", **fields}]}


def only(catalog):
    [plugin] = catalog.plugins
    return plugin


def skip_reason(catalog) -> str:
    assert catalog.plugins == ()
    [skipped] = catalog.skipped
    return skipped["reason"]


# -- where the file lives ------------------------------------------------------


@pytest.mark.parametrize("where", MARKETPLACE_FILES)
def test_a_marketplace_is_found_in_each_published_location(tmp_path, where):
    marketplace(tmp_path, GRAFANA, where=where)
    assert find_marketplace(tmp_path) == tmp_path / where


def test_a_tree_with_no_marketplace_has_none(tmp_path):
    (tmp_path / "README.md").write_text("no catalog here\n")
    assert find_marketplace(tmp_path) is None


def test_reading_a_tree_with_no_marketplace_says_where_it_looked(tmp_path):
    with pytest.raises(ValueError, match=r"\.claude-plugin/marketplace\.json"):
        read(tmp_path, {"plugins": []}, where=".github/other.json")


# -- the real shapes -----------------------------------------------------------


def test_grafanas_entries_become_plugins_sharing_one_fetch(tmp_path):
    """Seven plugins in one repository is the shape this whole layer exists for."""
    catalog = read(tmp_path, GRAFANA)

    assert catalog.name == "grafana-skills"
    assert [p.id for p in catalog.plugins] == [
        "grafana-lgtm@grafana",
        "grafana-dashboards@grafana",
        "grafana-alerting@grafana",
    ]
    assert len({p.fetch for p in catalog.plugins}) == 1
    assert catalog.skipped == ()


def test_a_grafana_entry_carries_its_metadata_and_its_skill_paths(tmp_path):
    plugin = read(tmp_path, GRAFANA).plugins[0]

    assert plugin.name == "grafana-lgtm"
    assert plugin.marketplace == "grafana"
    assert plugin.category == "grafana"
    assert plugin.tags == ("loki", "tempo", "mimir", "pyroscope")
    assert plugin.version == "0.1.0"
    assert plugin.description == "Query Loki, Tempo, Mimir and Pyroscope."
    assert plugin.globs == Globs(
        skills=(
            "skills/grafana-lgtm/loki",
            "skills/grafana-lgtm/tempo",
            "skills/grafana-lgtm/mimir",
        )
    )
    assert plugin.subdir == ""


def test_penpots_entry_leaves_its_components_to_its_manifest(tmp_path):
    """No `skills` and no `commands` on the entry: every kind stays None so the
    plugin's own `plugin.json` is what decides."""
    plugin = only(read(tmp_path, PENPOT))

    assert plugin.globs == Globs(skills=None, prompts=None, files=None)
    assert plugin.category == "design"
    assert plugin.tags == ("design", "penpot", "mcp")


def test_an_entrys_commands_become_prompt_patterns(tmp_path):
    plugin = only(read(tmp_path, entry(commands=["./prompts/brief.md", "commands"])))

    assert plugin.globs == Globs(prompts=("prompts/brief.md", "commands/*.md"))


def test_an_entrys_keywords_join_its_tags_as_labels(tmp_path):
    plugin = only(read(tmp_path, entry(tags=["design"], keywords=["penpot"])))

    assert plugin.labels == frozenset({"design", "penpot"})


# -- the source forms ----------------------------------------------------------


def test_a_relative_source_shares_the_marketplaces_own_fetch(tmp_path):
    here = config()
    address = parse_address("github://grafana/skills?ref=abc")
    plugin = only(read(tmp_path, entry(source="./plugins/a")))

    assert plugin.fetch == fetch_for(here, address)
    assert plugin.subdir == "plugins/a"


def test_a_relative_source_is_relative_to_the_marketplaces_subdir(tmp_path):
    catalog = read(
        tmp_path,
        {
            "plugins": [
                {"name": "here", "source": "./"},
                {"name": "below", "source": "./plugins/a"},
            ]
        },
        source="github://o/r//catalog?ref=abc",
    )

    assert [p.subdir for p in catalog.plugins] == ["catalog", "catalog/plugins/a"]


def test_a_relative_source_escaping_the_marketplace_is_skipped(tmp_path):
    assert "escapes" in skip_reason(read(tmp_path, entry(source="../elsewhere")))


def test_a_github_entry_resolves_through_the_declared_github_source(tmp_path):
    plugin = only(
        read(tmp_path, entry(source={"source": "github", "repo": "o/r", "ref": "v2"}))
    )

    assert plugin.fetch.url == "https://github.com/o/r"
    assert plugin.fetch.ref == "v2"
    assert plugin.subdir == ""


def test_a_github_entry_dedups_with_a_plugin_declared_on_the_same_repo(tmp_path):
    """Decision: a remote plugin is never edited here -- you declare your own
    against the same repository. That must cost no second clone."""
    here = config(plugins=[{"name": "mine", "source": "github://o/r?ref=v2"}])
    plugin = only(
        read(
            tmp_path,
            entry(source={"source": "github", "repo": "o/r", "ref": "v2"}),
            config=here,
        )
    )

    assert plugin.fetch == fetch_for(here, parse_address("github://o/r?ref=v2"))


def test_a_sha_wins_over_a_ref(tmp_path):
    plugin = only(
        read(
            tmp_path,
            entry(
                source={"source": "github", "repo": "o/r", "ref": "main", "sha": "dead"}
            ),
        )
    )

    assert plugin.fetch.ref == "dead"


def test_a_github_entry_with_no_github_source_declared_is_skipped(tmp_path):
    gitlab_only = Config.model_validate(
        {"sources": [{"name": "gitlab", "url": "git+https://gitlab.com"}]}
    )
    catalog = read(
        tmp_path,
        entry(source={"source": "github", "repo": "o/r"}),
        source="gitlab://g/catalog",
        config=gitlab_only,
    )

    assert skip_reason(catalog) == "no source for github.com is declared"


def test_a_url_entry_resolves_through_the_source_that_prefixes_it(tmp_path):
    plugin = only(
        read(
            tmp_path,
            entry(
                source={"source": "url", "url": "https://gitlab.com/group/repo.git"}
            ),
        )
    )

    assert plugin.fetch.url == "https://gitlab.com/group/repo.git"
    assert plugin.fetch.key == "gitlab://group/repo.git"


def test_a_url_entry_on_an_undeclared_host_is_skipped_naming_the_host(tmp_path):
    catalog = read(
        tmp_path,
        entry(source={"source": "url", "url": "https://git.example.org/o/r.git"}),
    )

    assert skip_reason(catalog) == "no source for git.example.org is declared"


def test_a_git_subdir_entry_keeps_the_path_as_its_subdir(tmp_path):
    plugin = only(
        read(
            tmp_path,
            entry(
                source={
                    "source": "git-subdir",
                    "url": "https://gitlab.com/group/repo",
                    "path": "plugins/x",
                    "ref": "v1",
                }
            ),
        )
    )

    assert plugin.fetch.url == "https://gitlab.com/group/repo"
    assert (plugin.subdir, plugin.fetch.ref) == ("plugins/x", "v1")


@pytest.mark.parametrize("kind", ["npm", "archive", "command", "carrier-pigeon"])
def test_a_source_type_this_server_does_not_install_is_skipped(tmp_path, kind):
    """`command` is never supported: nothing fetched is ever executed."""
    catalog = read(tmp_path, entry(source={"source": kind, "package": "x"}))

    assert skip_reason(catalog) == f"source type {kind} is not supported"


def test_an_entry_with_no_source_is_skipped(tmp_path):
    catalog = read(tmp_path, {"plugins": [{"name": "p"}]})

    assert "source" in skip_reason(catalog)


# -- the other skips -----------------------------------------------------------


def test_a_non_strict_entry_is_skipped(tmp_path):
    catalog = read(tmp_path, entry(strict=False))

    assert skip_reason(catalog) == "strict: false entries are not supported"


def test_a_strict_true_entry_is_ordinary(tmp_path):
    assert only(read(tmp_path, entry(strict=True))).name == "p"


@pytest.mark.parametrize("name", ["Not A Name", "", "-x", "a/b"])
def test_an_entry_whose_name_could_not_be_a_library_segment_is_skipped(tmp_path, name):
    catalog = read(tmp_path, {"plugins": [{"name": name, "source": "./"}]})

    assert "name" in skip_reason(catalog)


def test_a_skip_names_the_entry_it_skipped(tmp_path):
    catalog = read(tmp_path, entry(name="ghost", source={"source": "npm"}))

    assert catalog.skipped == (
        {"plugin": "ghost", "reason": "source type npm is not supported"},
    )


def test_a_nameless_entry_is_still_reported(tmp_path):
    catalog = read(tmp_path, {"plugins": [{"source": "./"}]})

    assert catalog.skipped[0]["plugin"] == "?"


def test_one_skipped_entry_does_not_cost_the_others(tmp_path):
    catalog = read(
        tmp_path,
        {
            "plugins": [
                {"name": "good", "source": "./"},
                {"name": "bad", "source": {"source": "npm", "package": "x"}},
            ]
        },
    )

    assert [p.name for p in catalog.plugins] == ["good"]
    assert [s["plugin"] for s in catalog.skipped] == ["bad"]


# -- a file that is not a marketplace ------------------------------------------


def test_a_file_that_is_not_json_is_an_error(tmp_path):
    with pytest.raises(ValueError, match=r"marketplace\.json"):
        read(tmp_path, "{not json")


def test_a_file_with_no_plugins_list_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="plugins"):
        read(tmp_path, {"name": "empty"})


def test_an_unnamed_marketplace_answers_to_its_library(tmp_path):
    assert read(tmp_path, {"plugins": []}).name == "grafana"
