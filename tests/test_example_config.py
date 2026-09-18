"""examples/config.yaml, the shape the image actually ships, against local git
repositories standing in for the four upstream libraries.

Each fixture repository publishes a ``.claude-plugin/marketplace.json`` the way
its upstream does -- grafana's entries name their own ``skills``, penpot's
plugin ships a ``plugin.json`` whose ``commands`` are what becomes a prompt,
superpowers' entry carries nothing but the keywords in its manifest -- so the
shipped config is exercised through marketplace entries, manifests and
commands-as-prompts end to end.

It also guards the ``shared/**/*``/``workflows/**/*`` fix for the
trailing-``**``-is-directories-only glob bug that would otherwise only be caught
by hand against the real 29-file penpot library. Local rather than against
github.com: the ``ref`` in the shipped config is a real upstream pin, which a
unit test must not depend on staying reachable or unchanged.
"""

import json
from pathlib import Path

import pygit2
import pytest
import yaml
from fastmcp import Client

from kubed.mcp_kb.config import Config
from kubed.mcp_kb.server import KnowledgeBase

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "examples" / "config.yaml"

# One skill's real URI per library the shipped config declares, from the
# BUILDERS fixtures below: grafana nests its skill a folder deeper than the
# other three, which is exactly the case an address built by hand gets wrong.
SKILL_URI_PER_LIBRARY = {
    "n8n": "skill://n8n/s/SKILL.md",
    "grafana": "skill://grafana/g/s/SKILL.md",
    "penpot": "skill://penpot/s/SKILL.md",
    "superpowers": "skill://superpowers/s/SKILL.md",
}

# The upstream repository each library's address names, and the fixture that
# stands in for it locally.
REPOS = {
    "n8n-io/skills": "n8n",
    "grafana/skills": "grafana",
    "penpot/penpot-ai-kit": "penpot",
    "obra/superpowers": "superpowers",
}

SIGNATURE = pygit2.Signature("Test", "test@example.com", 1700000000, 0)


def _skill(root: Path, *parts: str) -> None:
    path = root.joinpath(*parts, "SKILL.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {path.parent.name}\ndescription: d\n---\nBody.\n")


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _catalog(root: Path, name: str, *entries: dict) -> None:
    """The repository's ``marketplace.json``, with the entries it publishes."""
    _write(
        root,
        ".claude-plugin/marketplace.json",
        json.dumps({"name": name, "plugins": list(entries)}),
    )


def _manifest(root: Path, **fields) -> None:
    """The plugin's own ``plugin.json``: what it says it ships."""
    _write(root, ".claude-plugin/plugin.json", json.dumps(fields))


def _n8n(root: Path) -> None:
    _skill(root, "skills", "s")
    _catalog(
        root,
        "n8n",
        {"name": "n8n", "source": "./", "category": "automation", "tags": ["n8n"]},
    )


def _grafana(root: Path) -> None:
    """Several entries on one repository, each naming the skills it serves.

    grafana/skills really does publish seven that way, and `skills/template` is
    the one its catalog leaves out -- which is how an entry's own list keeps a
    repository's scaffolding out of the catalogue without a glob written here.
    """
    _skill(root, "skills", "g", "s")
    _skill(root, "skills", "h", "t")
    _skill(root, "skills", "template")
    _catalog(
        root,
        "grafana",
        {
            "name": "grafana-lgtm",
            "source": "./",
            "skills": ["./skills/g"],
            "category": "observability",
            "tags": ["lgtm"],
        },
        {
            "name": "grafana-cloud",
            "source": "./",
            "skills": ["./skills/h"],
            "category": "observability",
            "tags": ["cloud"],
        },
    )


def _penpot(root: Path) -> None:
    """The library that ships shared material, and a command in its manifest."""
    _skill(root, "skills", "s")
    _write(root, "shared/x.md", "shared\n")
    _write(root, "workflows/y.md", "workflow\n")
    # penpot/penpot-ai-kit keeps its commands under prompts/ and declares each
    # one in its manifest. The manifest is what decides: a sibling nobody
    # declared stays out, where the conventional prompt globs would take it.
    _write(
        root,
        "prompts/brief.md",
        "---\ndescription: Brief a design.\nargument-hint: [board]\n---\n"
        "Brief the board $ARGUMENTS.\n",
    )
    _write(root, "prompts/notes.md", "---\ndescription: Not ours to serve.\n---\nx\n")
    _manifest(root, name="penpot", commands=["./prompts/brief.md"])
    _catalog(
        root,
        "penpot",
        {"name": "penpot", "source": "./", "category": "design", "tags": ["penpot"]},
    )


def _superpowers(root: Path) -> None:
    """An entry that says nothing about itself: its manifest carries the words."""
    _skill(root, "skills", "s")
    _manifest(root, name="superpowers", keywords=["tdd", "planning"])
    _catalog(root, "superpowers", {"name": "superpowers", "source": "./"})


BUILDERS = {
    "n8n": _n8n,
    "grafana": _grafana,
    "penpot": _penpot,
    "superpowers": _superpowers,
}


def _repo(tmp_path: Path, name: str) -> None:
    """A one-commit repository laid out like the real library, with a marketplace."""
    root = tmp_path / name
    repo = pygit2.init_repository(str(root), bare=False, initial_head="main")
    BUILDERS[name](root)
    repo.index.add_all()
    repo.index.write()
    repo.create_commit(
        "refs/heads/main", SIGNATURE, SIGNATURE, "seed", repo.index.write_tree(), []
    )


def _rewritten_config(tmp_path: Path) -> Config:
    """The shipped config, with its ``github://`` addresses pointed at local repos.

    The `github` source becomes a `git+file://` source over ``tmp_path`` and
    every address keeps its repository name under it; ``?ref=`` is dropped,
    since the local repo has no such commit and its ``main`` tip stands in
    for whatever the real pin resolves to upstream.
    """
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    for source in raw["sources"]:
        source["url"] = f"git+file://{tmp_path}"
    for entry in [*raw.get("plugins", []), *raw["libraries"]]:
        if "source" not in entry:
            continue
        address = entry["source"].partition("?")[0]
        scheme, _, path = address.partition("://")
        name = REPOS[path]
        entry["source"] = f"{scheme}://{name}"
        if not (tmp_path / name).exists():
            _repo(tmp_path, name)
    return Config.model_validate(raw)


def _statuses(knowledge_base) -> dict[str, str]:
    return {
        f"{kind}:{name}": entry["status"]
        for kind in ("fetches", "plugins")
        for name, entry in knowledge_base.status[kind].items()
    }


def test_the_shipped_config_loads_the_shipped_shape(tmp_path):
    config = _rewritten_config(tmp_path)
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")

    statuses = _statuses(knowledge_base)
    assert all(status == "ok" for status in statuses.values()), statuses
    assert len(knowledge_base.resources.files("penpot")) == 2
    assert sorted(knowledge_base.status["libraries"]) == [
        "grafana", "n8n", "penpot", "superpowers",
    ]
    assert knowledge_base.status["libraries"]["penpot"]["plugins"] == [
        "penpot@penpot", "penpot-shared",
    ]
    assert not any("error" in lib for lib in knowledge_base.status["libraries"].values())


def test_a_marketplace_entry_brings_its_own_plugin_and_its_own_words(tmp_path):
    """grafana publishes one plugin per entry, each with the category and tags
    a scope selects it by -- none of which is written in this config."""
    knowledge_base = KnowledgeBase(_rewritten_config(tmp_path), tmp_path / "cache")

    assert knowledge_base.status["libraries"]["grafana"]["plugins"] == [
        "grafana-lgtm@grafana", "grafana-cloud@grafana",
    ]
    entry = knowledge_base.status["plugins"]["grafana-lgtm@grafana"]
    assert (entry["category"], entry["tags"]) == ("observability", ["lgtm"])
    # An entry that says nothing about itself is completed by its manifest: the
    # keywords plugin.json declares are what a scope can select superpowers by,
    # since its marketplace entry carries no category and no tags.
    superpowers = knowledge_base.status["plugins"]["superpowers@superpowers"]
    assert (superpowers["category"], superpowers["tags"], superpowers["keywords"]) == (
        None, [], ["planning", "tdd"],
    )


def test_an_entrys_skills_list_is_what_that_plugin_serves(tmp_path):
    """`skills/template` is in the repository and in no entry, so it is served
    by neither plugin -- and by nothing else in the library either."""
    knowledge_base = KnowledgeBase(_rewritten_config(tmp_path), tmp_path / "cache")

    served = {skill.name for skill in knowledge_base.index.visible()}
    assert "template" not in served
    assert {"s", "t"} <= served


def test_two_plugins_on_one_repository_share_one_clone(tmp_path):
    """penpot's marketplace and the `penpot-shared` plugin name one repository
    at one ref, which is one fetch: one clone under the cache, not two."""
    knowledge_base = KnowledgeBase(_rewritten_config(tmp_path), tmp_path / "cache")

    fetches = {
        entry["fetch"]
        for pid, entry in knowledge_base.status["plugins"].items()
        if pid in ("penpot@penpot", "penpot-shared")
    }
    assert len(fetches) == 1
    assert len(list((tmp_path / "cache" / "git").iterdir())) == 4


def test_a_declared_command_is_the_prompt_and_its_neighbour_is_not(tmp_path):
    """Every marketplace here is rooted at a repository root, where the
    conventional prompt globs (`prompts/**/*.md` and the rest) find whatever an
    upstream happens to keep. A plugin that declares its commands is telling us
    which of those files it publishes, and that is the one served -- in Claude's
    dialect, which is the dialect a command is written in.
    """
    knowledge_base = KnowledgeBase(_rewritten_config(tmp_path), tmp_path / "cache")

    prompts = {prompt.name: prompt for prompt in knowledge_base.prompts}
    assert list(prompts) == ["penpot_brief"]
    assert prompts["penpot_brief"].dialect == "claude"
    assert [a.name for a in prompts["penpot_brief"].arguments] == ["arguments"]


@pytest.mark.parametrize(("library", "uri"), sorted(SKILL_URI_PER_LIBRARY.items()))
async def test_a_skill_per_library_reads_back_at_its_real_uri(tmp_path, library, uri):
    """One `SKILL.md` per library, read through a real MCP client -- not a
    helper -- so a config that wires its marketplaces wrong, or a grammar
    regression that only shows up on a live read, fails here rather than only
    against the synthetic fixtures in test_address_space.py.

    grafana's skill sits a folder deeper than the other three libraries', so
    this also proves the library-plus-folder address is not just the
    library-plus-name shape the other three would pass with a bug in the
    folder segment.
    """
    knowledge_base = KnowledgeBase(_rewritten_config(tmp_path), tmp_path / "cache")
    async with Client(knowledge_base.mcp) as client:
        text = (await client.read_resource(uri))[0].text
    assert text == "---\nname: s\ndescription: d\n---\nBody.\n", library
