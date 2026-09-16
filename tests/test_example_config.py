"""examples/config.yaml, the shape the image actually ships, against a synthetic
tree standing in for the real fetched skills and prompts.

Pins the four-pack, five-source shape (`grafana-prompts` joins the `grafana`
library) and guards the ``shared/**/*``/``workflows/**/*`` fix for the
trailing-``**``-is-directories-only glob bug that would otherwise only be
caught by hand against the real 29-file penpot pack.
"""

from pathlib import Path

from mcp_school.config import load_config
from mcp_school.server import School

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "examples" / "config.yaml"
REAL_PROMPT = ROOT / "prompts" / "grafana" / "debug-logs.md"


def _skill(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {path.parent.name}\ndescription: d\n---\nBody.\n")


def _tree(tmp_path: Path) -> Path:
    """/skills/{n8n,grafana,penpot,superpowers} and /prompts/grafana, one tree."""
    skills = tmp_path / "skills"
    _skill(skills / "n8n" / "s" / "SKILL.md")
    _skill(skills / "grafana" / "g" / "s" / "SKILL.md")
    _skill(skills / "penpot" / "s" / "SKILL.md")
    _skill(skills / "superpowers" / "s" / "SKILL.md")

    (skills / "penpot" / "shared").mkdir(parents=True, exist_ok=True)
    (skills / "penpot" / "shared" / "x.md").write_text("shared\n")
    (skills / "penpot" / "workflows").mkdir(parents=True, exist_ok=True)
    (skills / "penpot" / "workflows" / "y.md").write_text("workflow\n")

    prompts = tmp_path / "prompts" / "grafana"
    prompts.mkdir(parents=True)
    (prompts / "debug-logs.md").write_text(REAL_PROMPT.read_text())

    return tmp_path


def _rewritten_config(tmp_path: Path) -> Path:
    """The shipped config, with its image-only roots pointed at ``tmp_path``."""
    text = EXAMPLE_CONFIG.read_text()
    text = text.replace("file:///skills", f"file://{tmp_path / 'skills'}")
    text = text.replace("file:///prompts", f"file://{tmp_path / 'prompts'}")
    out = tmp_path / "config.yaml"
    out.write_text(text)
    return out


def test_the_shipped_config_loads_the_shipped_shape(tmp_path):
    _tree(tmp_path)
    config = load_config(_rewritten_config(tmp_path))
    school = School(config, tmp_path / "cache")

    assert all(s["status"] == "ok" for s in school.status.values()), school.status
    assert len(school.resources.files("penpot")) == 2
    assert [p.name for p in school.prompts] == ["grafana_debug-logs"]

    libraries = sorted({s["library"] for s in school.status.values()})
    assert libraries == ["grafana", "n8n", "penpot", "superpowers"]
