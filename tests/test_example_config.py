"""examples/config.yaml, the shape the image actually ships, against local git
repositories standing in for the four upstream packs.

Pins the four-source shape and guards the ``shared/**/*``/``workflows/**/*``
fix for the trailing-``**``-is-directories-only glob bug that would otherwise
only be caught by hand against the real 29-file penpot pack. Local rather than
against github.com: the ``ref`` in the shipped config is a real upstream pin,
which a unit test must not depend on staying reachable or unchanged.
"""

from pathlib import Path

import pygit2
import yaml

from kubed.mcp_kb.config import Config
from kubed.mcp_kb.server import KnowledgeBase

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "examples" / "config.yaml"

SIGNATURE = pygit2.Signature("Test", "test@example.com", 1700000000, 0)


def _skill(root: Path, *parts: str) -> None:
    path = root.joinpath(*parts, "SKILL.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {path.parent.name}\ndescription: d\n---\nBody.\n")


def _penpot(root: Path) -> None:
    """The one pack that also references shared material outside its skills."""
    _skill(root, "skills", "s")
    (root / "shared").mkdir()
    (root / "shared" / "x.md").write_text("shared\n")
    (root / "workflows").mkdir()
    (root / "workflows" / "y.md").write_text("workflow\n")
    # penpot/penpot-ai-kit really does have one of these at its repo root, and
    # the day an upstream adds frontmatter to a file in it, an `include` that
    # names no prompts glob starts serving whatever is in there.
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


def _repo(tmp_path: Path, name: str) -> str:
    """A one-commit repository laid out like the real pack, as a ``git+file://`` URL."""
    root = tmp_path / name
    repo = pygit2.init_repository(str(root), bare=False, initial_head="main")
    BUILDERS[name](root)
    repo.index.add_all()
    repo.index.write()
    repo.create_commit(
        "refs/heads/main", SIGNATURE, SIGNATURE, "seed", repo.index.write_tree(), []
    )
    return f"git+file://{root}"


def _rewritten_config(tmp_path: Path) -> Config:
    """The shipped config, with its ``github://`` sources pointed at local repos.

    ``ref`` is dropped: the local repo has no such commit, and its ``main`` tip
    stands in for whatever the real pin resolves to upstream.
    """
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    for source in raw["sources"]:
        source["url"] = _repo(tmp_path, source["name"])
        source.pop("ref", None)
    return Config.model_validate(raw)


def test_the_shipped_config_loads_the_shipped_shape(tmp_path):
    config = _rewritten_config(tmp_path)
    knowledge_base = KnowledgeBase(config, tmp_path / "cache")

    assert all(s["status"] == "ok" for s in knowledge_base.status.values()), knowledge_base.status
    assert len(knowledge_base.resources.files("penpot")) == 2

    libraries = sorted({s["library"] for s in knowledge_base.status.values()})
    assert libraries == ["grafana", "n8n", "penpot", "superpowers"]


def test_the_shipped_config_serves_no_prompt_it_did_not_ask_for(tmp_path):
    """Every source here is rooted at a repository root, where the conventional
    prompt globs (`prompts/**/*.md` and the rest) find whatever an upstream
    happens to keep. A source that names one include kind still gets the
    defaults for the others, so each one says `prompts: []` and means it.
    """
    knowledge_base = KnowledgeBase(_rewritten_config(tmp_path), tmp_path / "cache")

    assert knowledge_base.prompts == ()
