"""examples/config.yaml, the shape the image actually ships, against local git
repositories standing in for the four upstream libraries.

Pins the marketplace-library shape and guards the ``shared/**/*``/``workflows/**/*``
fix for the trailing-``**``-is-directories-only glob bug that would otherwise
only be caught by hand against the real 29-file penpot library. Local rather than
against github.com: the ``ref`` in the shipped config is a real upstream pin,
which a unit test must not depend on staying reachable or unchanged.
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


def _marketplace(root: Path, name: str) -> None:
    """One entry at the repository root, its commands declared and empty.

    The entry says what it ships, as a real catalog does: `commands: []`
    keeps whatever the repository keeps under `prompts/` out of the
    catalogue, because a marketplace plugin is never edited here.
    """
    catalog = root / ".claude-plugin" / "marketplace.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps(
            {
                "name": name,
                "plugins": [{"name": name, "source": "./", "commands": []}],
            }
        )
    )


def _penpot(root: Path) -> None:
    """The one library that also references shared material outside its skills."""
    _skill(root, "skills", "s")
    (root / "shared").mkdir()
    (root / "shared" / "x.md").write_text("shared\n")
    (root / "workflows").mkdir()
    (root / "workflows" / "y.md").write_text("workflow\n")
    # penpot/penpot-ai-kit really does have one of these at its repo root, and
    # the day an upstream adds frontmatter to a file in it, an entry that names
    # no commands starts serving whatever is in there.
    (root / "prompts").mkdir()
    (root / "prompts" / "brief.md").write_text(
        "---\ndescription: Not ours to serve.\n---\nDo the thing.\n"
    )


BUILDERS = {
    "n8n": lambda root: _skill(root, "skills", "s"),
    "grafana": lambda root: _skill(root, "skills", "g", "s"),
    "penpot": _penpot,
    "superpowers": lambda root: _skill(root, "skills", "s"),
}


def _repo(tmp_path: Path, name: str) -> None:
    """A one-commit repository laid out like the real library, with a marketplace."""
    root = tmp_path / name
    repo = pygit2.init_repository(str(root), bare=False, initial_head="main")
    BUILDERS[name](root)
    _marketplace(root, name)
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


def test_the_shipped_config_serves_no_prompt_it_did_not_ask_for(tmp_path):
    """Every marketplace here is rooted at a repository root, where the
    conventional prompt globs (`prompts/**/*.md` and the rest) find whatever an
    upstream happens to keep. An entry's own `commands` is what it serves, and
    the fixture catalogs say `commands: []` and mean it.
    """
    knowledge_base = KnowledgeBase(_rewritten_config(tmp_path), tmp_path / "cache")

    assert knowledge_base.prompts == ()


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
