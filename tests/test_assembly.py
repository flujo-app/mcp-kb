"""Libraries assembled from marketplaces, named plugins and selectors.

The catalogue's unit is the plugin, and a library is a saved query over
plugins: a marketplace's entries, a list of names, a selector, or their union.
These tests build real ``KnowledgeBase``s over local git repositories --
marketplaces are published in repositories -- and read the result back
through ``/health`` and a real MCP client.
"""

import json
import os
from pathlib import Path

import httpx
import pygit2
import pytest
from fastmcp import Client

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.catalogue.index import PluginRecord, now
from kubed.mcp_kb.catalogue.snapshot import Library, build_snapshot
from kubed.mcp_kb.config import Config
from kubed.mcp_kb.mcp.pins import what_is_wrong
from kubed.mcp_kb.mcp.scope import Scope
from tests.conftest import fake_plugin

pytestmark = pytest.mark.unit

SIGNATURE = pygit2.Signature("Test", "test@example.com", 1700000000, 0)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _skill(root, rel, name):
    _write(root, f"{rel}/SKILL.md", f"---\nname: {name}\ndescription: {name}.\n---\n")


def _commit(root, message="seed"):
    """Commit everything under ``root``, initialising the repository if need be."""
    try:
        repo = pygit2.Repository(str(root))
        parents = [repo.head.target]
    except pygit2.GitError:
        repo = pygit2.init_repository(str(root), bare=False, initial_head="main")
        parents = []
    repo.index.add_all()
    repo.index.write()
    repo.create_commit(
        "refs/heads/main", SIGNATURE, SIGNATURE, message, repo.index.write_tree(), parents
    )


def _catalog(root, *entries):
    _write(
        root,
        ".claude-plugin/marketplace.json",
        json.dumps({"name": "market", "plugins": list(entries)}),
    )


@pytest.fixture
def market(tmp_path):
    """A repository publishing two plugins, each in its own folder with a category."""
    root = tmp_path / "market"
    _skill(root, "lgtm/skills/loki", "loki")
    _skill(root, "k6/skills/load", "load")
    _write(root, "k6/commands/run.md", "---\ndescription: Run.\n---\nRun $0.\n")
    _catalog(
        root,
        {
            "name": "lgtm",
            "source": "./lgtm",
            "category": "observability",
            "tags": ["logs"],
        },
        {"name": "k6", "source": "./k6", "category": "testing", "keywords": ["load"]},
        {"name": "cli", "source": {"source": "npm", "package": "x"}},
    )
    _commit(root)
    return root


def _config(tmp_path, **extra):
    return Config.model_validate(
        {
            "sources": [{"name": "lab", "url": f"git+file://{tmp_path}"}],
            **extra,
        }
    )


async def _health(knowledge_base):
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as http:
        return (await http.get("/health")).json()


async def _skills(knowledge_base):
    """Every served skill's URI: the full listing, which is the index's addresses."""
    listing = knowledge_base.catalogue.entries(full=True)
    return sorted(e.uri for e in listing if e.uri.endswith("/SKILL.md"))


async def _read(knowledge_base, uri):
    async with Client(knowledge_base.mcp) as client:
        return (await client.read_resource(uri))[0].text


async def test_a_marketplace_library_lists_its_entries_as_plugins(market, tmp_path):
    """Each entry is a plugin id'd `<entry>@<library>`, carrying the entry's
    own category and labels; the uninstallable one is skipped, and said so."""
    config = _config(tmp_path, libraries=[{"name": "obs", "source": "lab://market"}])
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    library = health["libraries"]["obs"]
    assert library["plugins"] == ["lgtm@obs", "k6@obs"]
    assert library["skills"] == 2 and library["prompts"] == 1
    assert library["skipped"] == [
        {"plugin": "cli", "reason": "source type npm is not supported"}
    ]
    lgtm = health["plugins"]["lgtm@obs"]
    assert lgtm["category"] == "observability"
    assert lgtm["tags"] == ["logs"]
    assert lgtm["root"] == "lgtm"
    assert lgtm["libraries"] == ["obs"]
    assert health["plugins"]["k6@obs"]["keywords"] == ["load"]
    assert "loki." in await _read(kb, "skill://obs/loki/SKILL.md")
    assert "load." in await _read(kb, "skill://obs/load/SKILL.md")
    async with Client(kb.mcp) as client:
        rendered = await client.get_prompt("obs_run", {"arguments": "fast"})
    assert rendered.messages[0].content.text == "Run fast.\n"


async def test_a_selector_regroups_a_marketplace_plugin_under_its_own_name(
    market, tmp_path
):
    """Phase two runs over every plugin, marketplace-derived included: a selector
    picks `lgtm@obs` into `logs` as well, and the skill serves at both bases."""
    config = _config(
        tmp_path,
        libraries=[
            {"name": "obs", "source": "lab://market"},
            {"name": "logs", "pluginSelector": {"tags": ["logs"]}},
        ],
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    assert health["libraries"]["logs"]["plugins"] == ["lgtm@obs"]
    assert health["plugins"]["lgtm@obs"]["libraries"] == ["obs", "logs"]
    assert await _skills(kb) == [
        "skill://logs/loki/SKILL.md",
        "skill://obs/load/SKILL.md",
        "skill://obs/loki/SKILL.md",
    ]
    assert "loki." in await _read(kb, "skill://logs/loki/SKILL.md")
    assert "loki." in await _read(kb, "skill://obs/loki/SKILL.md")


async def test_a_named_marketplace_plugin_adds_beside_a_library(market, tmp_path):
    """`plugins: [k6@obs]` is resolved once the marketplace has been read; a
    name it did not publish is the library's error, and the rest serves."""
    config = _config(
        tmp_path,
        libraries=[
            {"name": "obs", "source": "lab://market"},
            {"name": "perf", "plugins": ["k6@obs", "nope@obs"]},
        ],
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    perf = health["libraries"]["perf"]
    assert perf["plugins"] == ["k6@obs"]
    assert perf["error"] == "no plugin named 'nope@obs'; obs publishes: k6, lgtm"
    assert "load." in await _read(kb, "skill://perf/load/SKILL.md")


async def test_two_plugins_on_one_repository_at_one_ref_make_one_clone(
    market, tmp_path
):
    """A declared plugin against the marketplace's repository shares its fetch:
    one entry under `<cache>/git`, and one fetch in `/health`."""
    config = _config(
        tmp_path,
        plugins=[{"name": "mine", "source": "lab://market//k6", "tags": ["own"]}],
        libraries=[
            {"name": "obs", "source": "lab://market"},
            {"name": "own", "plugins": ["mine"]},
        ],
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    assert list(health["fetches"]) == ["lab://market"]
    assert [p.name for p in (tmp_path / "cache" / "git").iterdir()] == [
        kb.fetches[0].slug
    ]
    assert health["plugins"]["mine"]["fetch"] == "lab://market"
    assert "load." in await _read(kb, "skill://own/load/SKILL.md")


async def test_a_library_whose_marketplace_is_missing_says_so(market, tmp_path):
    config = _config(
        tmp_path,
        libraries=[{"name": "obs", "source": "lab://market//lgtm"}],
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    assert health["libraries"]["obs"] == {
        "description": "",
        "plugins": [],
        "skills": 0,
        "prompts": 0,
        "files": 0,
        "error": "no marketplace.json under lgtm",
    }
    assert str(tmp_path) not in json.dumps(health)


async def test_a_credential_in_an_entry_never_reaches_health(market, tmp_path):
    """A marketplace is fetched content, and its skip reasons are published
    unauthenticated. Two hazards, one per entry: a `url` this server has no
    source for, whose message is built from the host alone; and a `source`
    type nothing installs, whose message quotes what the entry wrote -- the
    class of message a future one will join, and what the scrub at the library
    boundary is for."""
    _catalog(
        market,
        {
            "name": "sneaky",
            "source": {
                "source": "url",
                "url": "https://user:s3cret@git.example.org/o/r.git",
            },
        },
        {
            "name": "sneakier",
            "source": {"source": "https://user:s3cret@git.example.org/o/r"},
        },
    )
    _commit(market, "credentials")
    config = _config(tmp_path, libraries=[{"name": "obs", "source": "lab://market"}])
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    assert health["libraries"]["obs"]["skipped"] == [
        {"plugin": "sneaky", "reason": "no source for git.example.org is declared"},
        {
            "plugin": "sneakier",
            "reason": "source type https://***@git.example.org/o/r is not supported",
        },
    ]
    assert "s3cret" not in json.dumps(health)
    assert "user:" not in json.dumps(health)


async def test_an_unreadable_marketplace_is_reported_without_the_cache_path(
    market, tmp_path
):
    """An OSError quotes the file's absolute path, which for a fetched tree is
    a cache path; the library's error must name the file and nothing above the
    plugin root. The export is made unreadable after one build, and the restart
    -- which reuses the fetch and re-reads the catalog off it -- meets it."""
    config = _config(tmp_path, libraries=[{"name": "obs", "source": "lab://market"}])
    first = KnowledgeBase(config, tmp_path / "cache")
    root = Path(first._fetches["lab://market"].root)
    catalog = root / ".claude-plugin" / "marketplace.json"
    catalog.chmod(0o000)
    if os.access(catalog, os.R_OK):
        pytest.skip("running as root: the file is readable whatever its mode")

    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    error = health["libraries"]["obs"]["error"]
    assert "marketplace.json" in error and "Permission denied" in error
    assert str(tmp_path) not in json.dumps(health)


async def test_an_unreadable_manifest_is_reported_without_the_cache_path(
    market, tmp_path
):
    """The same leak, on the other file a plugin root is read for.

    ``read_manifest``'s OSError quotes the absolute path it failed on, and a
    plugin's error is published in ``/health`` exactly as a library's is. Built
    once, made unreadable, then met on the restart that reuses the fetch."""
    _write(market, "lgtm/.claude-plugin/plugin.json", '{"version": "1.0"}')
    _commit(market, "manifest")
    config = _config(
        tmp_path,
        plugins=[{"name": "lgtm", "source": "lab://market//lgtm"}],
        libraries=[{"name": "obs", "plugins": ["lgtm"]}],
    )
    first = KnowledgeBase(config, tmp_path / "cache")
    manifest = (
        Path(first._fetches["lab://market"].root)
        / "lgtm"
        / ".claude-plugin"
        / "plugin.json"
    )
    manifest.chmod(0o000)
    if os.access(manifest, os.R_OK):
        pytest.skip("running as root: the file is readable whatever its mode")

    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    error = health["plugins"]["lgtm"]["error"]
    assert "plugin.json" in error and "Permission denied" in error
    assert str(tmp_path) not in json.dumps(health)


def test_a_plugin_status_never_carries_a_null_error(tmp_path):
    """`PluginStatus.error` is a string: an ok record with no root -- a shape
    nothing builds, but the index could hold -- says so in words."""
    plugin = fake_plugin("p", tmp_path)
    record = PluginRecord(
        id="p", fetch=plugin.fetch.key, root=None, status="ok", error=None,
        built=now(), skills=(), prompts=(), files=(), skill_dirs=(),
    )
    snapshot = build_snapshot(
        Config(), [plugin], [Library("lib", "", ("p",))], {}, {"p": record}, 0
    )

    assert snapshot.status["plugins"]["p"]["error"] == "not harvested"


async def test_a_refresh_of_a_marketplace_fetch_serves_an_entry_it_gained(
    market, tmp_path
):
    """The marketplace is re-read off the rebuilt tree, so a new entry -- and
    the fetch it may bring -- is served after one refresh, with no restart."""
    config = _config(tmp_path, libraries=[{"name": "obs", "source": "lab://market"}])
    kb = KnowledgeBase(config, tmp_path / "cache")
    assert "skill://obs/tempo/SKILL.md" not in await _skills(kb)

    _skill(market, "tempo/skills/tempo", "tempo")
    _catalog(
        market,
        {"name": "lgtm", "source": "./lgtm", "category": "observability"},
        {"name": "k6", "source": "./k6", "category": "testing"},
        {"name": "tempo", "source": "./tempo", "category": "observability"},
    )
    _commit(market, "add tempo")

    assert kb.refresh() == ["lab://market"]
    health = await _health(kb)
    assert health["libraries"]["obs"]["plugins"] == ["lgtm@obs", "k6@obs", "tempo@obs"]
    assert "tempo." in await _read(kb, "skill://obs/tempo/SKILL.md")
    assert kb.generation == 1


async def _reindex(knowledge_base):
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as http:
        return (await http.post("/reindex")).json()


def _elsewhere(name, tmp_path):
    """An entry on another repository under the same declared source."""
    return {
        "name": name,
        "source": {"source": "url", "url": f"file://{tmp_path / name}"},
    }


async def test_a_refresh_reports_the_fetch_a_gained_entry_brought(market, tmp_path):
    """`rebuilt` was appended to only inside the loop over *this* generation's
    fetches, and the repository a new entry points at is materialised after it,
    by `_assemble`. It was cloned, so it is reported: `/reindex`'s `rebuilt`
    and one log line are the only places an operator sees that it was."""
    for name in ("other", "third"):
        _skill(tmp_path / name, f"skills/{name}", name)
        _commit(tmp_path / name)
    config = _config(tmp_path, libraries=[{"name": "obs", "source": "lab://market"}])
    kb = KnowledgeBase(config, tmp_path / "cache")
    assert kb.refresh() == []

    lgtm = {"name": "lgtm", "source": "./lgtm"}
    _catalog(market, lgtm, _elsewhere("other", tmp_path))
    _commit(market, "add other")
    assert kb.refresh() == ["lab://market", "lab://other"]
    assert "other." in await _read(kb, "skill://obs/other/SKILL.md")

    _catalog(market, lgtm, _elsewhere("other", tmp_path), _elsewhere("third", tmp_path))
    _commit(market, "add third")
    body = await _reindex(kb)
    assert body["rebuilt"] == ["lab://market", "lab://other", "lab://third"]
    assert "third." in await _read(kb, "skill://obs/third/SKILL.md")


@pytest.fixture
def manifested(tmp_path):
    """A repository whose one entry is bare -- no tags, no category -- while
    the plugin's own `plugin.json` carries the keywords, a description and a
    version, the way obra/superpowers publishes."""
    root = tmp_path / "market"
    _skill(root, "sp/skills/tdd", "tdd")
    _write(
        root,
        "sp/.claude-plugin/plugin.json",
        json.dumps(
            {"keywords": ["tdd", "testing"], "description": "From the manifest.",
             "version": "9.9"}
        ),
    )
    _catalog(root, {"name": "sp", "source": "./sp", "version": "1.0"})
    _commit(root)
    return root


async def test_a_manifests_keywords_join_the_plugins_labels(manifested, tmp_path):
    """Tagged nowhere but in its plugin.json, the plugin is still selectable:
    a `pluginSelector` on the keyword picks it, `?tags=` on a real request
    admits it, and /health shows the merged labels. The entry's version beats
    the manifest's; the description it left empty is the manifest's."""
    import threading

    import uvicorn
    from fastmcp.client.transports import StreamableHttpTransport

    from tests.test_header_scope import _free_port

    config = _config(
        tmp_path,
        libraries=[
            {"name": "obs", "source": "lab://market"},
            {"name": "tests", "pluginSelector": {"tags": ["tdd"]}},
        ],
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    entry = health["plugins"]["sp@obs"]
    assert entry["keywords"] == ["tdd", "testing"] and entry["tags"] == []
    assert entry["version"] == "1.0", "the marketplace entry is never overridden"
    assert health["libraries"]["tests"]["plugins"] == ["sp@obs"]
    assert kb._plugins[0].description == "From the manifest."

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(kb.mcp.http_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    try:
        async with Client(
            StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp?tags=tdd")
        ) as client:
            names = sorted(r.name for r in await client.list_resources())
        async with Client(
            StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp?tags=testing,tdd")
        ) as client:
            both = sorted(r.name for r in await client.list_resources())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert names == ["obs/_index.md", "tests/_index.md"]
    assert both == names


async def test_a_declared_plugin_keeps_its_own_words_over_the_manifests(
    manifested, tmp_path
):
    """A declared value always wins; the manifest only fills what is empty."""
    config = _config(
        tmp_path,
        plugins=[
            {
                "name": "mine",
                "source": "lab://market//sp",
                "description": "Mine.",
                "keywords": ["own"],
            }
        ],
        libraries=[{"name": "own", "plugins": ["mine"]}],
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    (mine,) = kb._plugins
    assert mine.description == "Mine."
    assert health["plugins"]["mine"]["keywords"] == ["own", "tdd", "testing"]
    assert health["plugins"]["mine"]["version"] == "9.9", "left empty, so filled"


async def test_a_cold_start_reuses_the_index_and_re_reads_the_marketplace(
    market, tmp_path, monkeypatch
):
    """Nothing is cloned or harvested on a restart; the marketplace's entries
    are re-derived from the tree already on disk."""
    from kubed.mcp_kb import server
    from kubed.mcp_kb.catalogue import snapshot

    config = _config(tmp_path, libraries=[{"name": "obs", "source": "lab://market"}])
    first = KnowledgeBase(config, tmp_path / "cache")

    def must_not_run(*args, **kwargs):
        raise AssertionError("a cold start must reuse the index")

    # server.py imports build_plugin by name, so it is patched where it is
    # called from; materialise is called inside snapshot.build_fetch.
    monkeypatch.setattr(snapshot, "materialise", must_not_run)
    monkeypatch.setattr(server, "build_plugin", must_not_run)
    second = KnowledgeBase(config, tmp_path / "cache")

    assert second.status["libraries"]["obs"]["plugins"] == ["lgtm@obs", "k6@obs"]
    assert await _skills(second) == await _skills(first)


# -- the containment guard on a plugin or marketplace root ---------------------


@pytest.fixture
def linked(tmp_path):
    """A ``file://`` tree holding symlinks: three out of it, two inside it.

    A `git` export cannot carry one -- it writes a symlink as a regular file
    holding its target text -- so a local tree is the reachable case, and a
    ConfigMap mount, which is symlinks the whole way down, is the reason the
    in-root ones have to keep working.
    """
    outside = tmp_path / "outside"
    _skill(outside, "skills/leak", "leak")
    _catalog(outside, {"name": "foreign", "source": "./"})
    _write(outside, "plugin.json", json.dumps({"keywords": ["foreign"]}))

    tree = tmp_path / "tree"
    _skill(tree, "good/skills/ok", "ok")
    _skill(tree, "manifested/skills/m", "m")
    (tree / "manifested" / "plugin.json").symlink_to(outside / "plugin.json")
    # A ConfigMap mount, in the shape Kubernetes writes it: the key is a link
    # to `..data`, which is a link to the current timestamped directory.
    _skill(tree, "mounted/skills/mine", "mine")
    _write(tree, "mounted/..2026_09_18/plugin.json", json.dumps({"keywords": ["m"]}))
    (tree / "mounted" / "..data").symlink_to("..2026_09_18")
    (tree / "mounted" / "plugin.json").symlink_to("..data/plugin.json")
    _write(tree, "market/.gitkeep", "")
    (tree / "market" / ".claude-plugin").mkdir(parents=True)
    (tree / "market" / ".claude-plugin" / "marketplace.json").symlink_to(
        outside / ".claude-plugin" / "marketplace.json"
    )
    (tree / "link").symlink_to(outside)
    (tree / "inner").symlink_to(tree / "good")
    return tree


def _linked_config(tree, **extra):
    return Config.model_validate(
        {
            "plugins": [
                {"name": "good", "source": f"file://{tree}//good"},
                {"name": "escaped", "source": f"file://{tree}//link"},
            ],
            "libraries": [{"name": "kit", "plugins": ["good", "escaped"]}],
            **extra,
        }
    )


async def test_a_subdir_symlinked_out_of_the_tree_fails_that_plugin(linked, tmp_path):
    """The escape is the plugin's own failure, named, and the library serves on.

    Reachable only through a link: `..` is refused by ``parse_address``. Left
    unguarded, ``root.is_dir()`` is true for the link's target and everything
    under it becomes "contained" relative to it -- so the tree next door is
    served as this plugin.
    """
    kb = KnowledgeBase(_linked_config(linked), tmp_path / "cache")
    health = await _health(kb)

    assert health["plugins"]["escaped"]["status"] == "failed"
    assert health["plugins"]["escaped"]["error"] == (
        f"link is not a directory in file://{linked}"
    )
    assert await _skills(kb) == ["skill://kit/ok/SKILL.md"]
    assert "leak" not in json.dumps(await _health(kb))


async def test_a_marketplace_subdir_symlinked_out_of_the_tree_is_the_librarys_error(
    linked, tmp_path
):
    """The same guard on the other root, where an escape would have this server
    ingest somebody else's whole catalogue as a library of ours."""
    config = _linked_config(
        linked, libraries=[{"name": "far", "source": f"file://{linked}//link"}]
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    assert health["libraries"]["far"] == {
        "description": "",
        "plugins": [],
        "skills": 0,
        "prompts": 0,
        "files": 0,
        "error": f"link is not a directory in file://{linked}",
    }
    assert await _skills(kb) == []


async def test_a_marketplace_json_symlinked_out_of_the_tree_is_not_read(
    linked, tmp_path
):
    """``is_file()`` follows a link, so a tree could point at a catalogue it
    does not hold. The library reports no marketplace rather than reading it."""
    config = _linked_config(
        linked, libraries=[{"name": "near", "source": f"file://{linked}//market"}]
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    assert health["libraries"]["near"]["error"] == "no marketplace.json under market"
    assert health["libraries"]["near"]["plugins"] == []


async def test_a_plugin_json_symlinked_out_of_the_root_is_not_read(linked, tmp_path):
    """The plugin serves what it holds, uncompleted: an external manifest's
    description and keywords are not its publisher's word on it."""
    config = Config.model_validate(
        {
            "plugins": [{"name": "m", "source": f"file://{linked}//manifested"}],
            "libraries": [{"name": "kit", "plugins": ["m"]}],
        }
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    assert health["plugins"]["m"]["status"] == "ok"
    assert health["plugins"]["m"]["keywords"] == []
    assert await _skills(kb) == ["skill://kit/m/SKILL.md"]


async def test_an_in_root_symlinked_subdir_and_manifest_still_serve(linked, tmp_path):
    """The ConfigMap case, which the guard must not cost: every path of a
    mounted volume is a symlink, and both roots resolve inside the tree."""
    config = Config.model_validate(
        {
            "plugins": [
                {"name": "inner", "source": f"file://{linked}//inner"},
                {"name": "mounted", "source": f"file://{linked}//mounted"},
            ],
            "libraries": [{"name": "kit", "plugins": ["inner", "mounted"]}],
        }
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    health = await _health(kb)

    assert health["plugins"]["inner"]["status"] == "ok"
    assert health["plugins"]["mounted"]["keywords"] == ["m"]
    assert await _skills(kb) == [
        "skill://kit/mine/SKILL.md",
        "skill://kit/ok/SKILL.md",
    ]


# -- a scope on a library whose marketplace could not be read -------------------


async def test_a_library_whose_marketplace_failed_is_not_refused_a_category_or_tag(
    market, tmp_path
):
    """A declared library is refused for what it *names*, never for what it
    happens to be serving -- and a marketplace's categories and tags exist
    only in the snapshot, so a library that could not be read has none of
    either. A serving library still refuses one it does not carry.
    """
    config = _config(
        tmp_path,
        libraries=[
            {"name": "obs", "source": "lab://market"},
            {"name": "far", "source": "lab://nowhere"},
        ],
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    assert kb.status["libraries"]["far"]["error"]

    for narrower in ({"categories": ["whatever"]}, {"tags": ["whatever"]}):
        scope = Scope.parse(library="far", **narrower)
        assert what_is_wrong(scope, config, kb.snapshot) is None

    problem = what_is_wrong(
        Scope.parse(library="obs", categories=["whatever"]), config, kb.snapshot
    )
    assert problem.startswith("The scope names category 'whatever'")
    problem = what_is_wrong(
        Scope.parse(library="obs", tags=["whatever"]), config, kb.snapshot
    )
    assert problem.startswith("The scope names tag 'whatever'")


# -- a marketplace or a manifest edited in place -------------------------------


async def test_editing_a_local_marketplace_is_picked_up_by_a_refresh(tmp_path):
    """A `file://` marketplace is the case this whole shape exists for -- a
    mounted volume, edited in place -- and `.claude-plugin/` holds the only
    file that changes when an entry is added. A fingerprint walk that could not
    see it left the fingerprint where it was, so a configured `refresh:` never
    re-read the catalogue and only a restart or `/reindex` picked the entry up.
    """
    root = tmp_path / "market"
    _skill(root, "lgtm/skills/loki", "loki")
    _skill(root, "k6/skills/load", "load")
    lgtm = {"name": "lgtm", "source": "./lgtm"}
    _catalog(root, lgtm)
    config = Config.model_validate(
        {"libraries": [{"name": "obs", "source": f"file://{root}"}]}
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    key = f"file://{root}"
    assert await _skills(kb) == ["skill://obs/loki/SKILL.md"]
    before = kb.status["fetches"][key]["fingerprint"]

    _catalog(root, lgtm, {"name": "k6", "source": "./k6"})

    assert kb.refresh() == [key]
    assert kb.status["fetches"][key]["fingerprint"] != before
    assert await _skills(kb) == [
        "skill://obs/load/SKILL.md",
        "skill://obs/loki/SKILL.md",
    ]


async def test_nothing_under_claude_plugin_is_harvested_or_served(tmp_path):
    """The fingerprint walk sees that directory; the harvest must not. A
    `marketplace.json` and a `plugin.json` say *what* to serve and are not
    themselves a skill, a prompt or a library file -- not even under the
    widest globs a config can write."""
    root = tmp_path / "tree"
    _skill(root, "skills/real", "real")
    _write(root, "prompts/ok.md", "---\ndescription: Fine.\n---\nFine.\n")
    _catalog(root, {"name": "x", "source": "./"})
    _write(root, ".claude-plugin/plugin.json", json.dumps({"keywords": ["k"]}))
    _skill(root, ".claude-plugin/skills/sneaky", "sneaky")
    _write(root, ".claude-plugin/prompts/sneaky.md", "---\ndescription: d\n---\nNo.\n")
    config = Config.model_validate(
        {
            "plugins": [
                {
                    "name": "p",
                    "source": f"file://{root}",
                    "skills": ["**/SKILL.md"],
                    "prompts": ["prompts/**/*.md"],
                    "files": ["**/*"],
                }
            ],
            "libraries": [{"name": "lib", "plugins": ["p"]}],
        }
    )
    kb = KnowledgeBase(config, tmp_path / "cache")

    assert [s.name for s in kb.index.visible()] == ["real"]
    assert [p.name for p in kb.snapshot.prompts] == ["lib_ok"]
    assert kb.resources.files("lib") == ["prompts/ok.md"]
    assert ".claude-plugin" not in json.dumps(await _health(kb))
