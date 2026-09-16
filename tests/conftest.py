"""Shared fixtures: a synthetic skills tree covering both nesting depths."""

import pytest

from mcp_school import harvest
from mcp_school.config import Include
from mcp_school.prompts import load_prompts
from mcp_school.skills import PackResources, load_skills

FLAT = {"alpha": "First skill.", "beta": "Second skill."}
NESTED = {"plugin-a": {"gamma": "Third skill."}, "plugin-b": {"delta": "Fourth skill."}}

# Each synthetic pack is its own source root, so the include glob is relative
# to that pack's directory rather than to the tree root. "**/SKILL.md" finds a
# skill at any depth under a pack -- flatsource lays its directly under itself,
# deepsource one level deeper under a plugin -- and harvest.group_of() (via
# SKILL_ROOTS) sorts flat from nested from there, exactly as the old rglob over
# the whole tree did.
SKILLS_INCLUDE = Include(skills=["**/SKILL.md"], files=["**/*"])
PACK_INCLUDES = {"flatsource": SKILLS_INCLUDE, "deepsource": SKILLS_INCLUDE}
PROMPT_INCLUDE = Include(prompts=["*.md"])


def load_all_skills(root):
    """Harvest and load every synthetic pack under ``root``, as one catalogue."""
    skills = []
    for pack, include in PACK_INCLUDES.items():
        pack_root = root / pack
        if pack_root.is_dir():
            dirs = harvest.skill_dirs(pack_root, include)
            skills += load_skills(dirs, pack=pack, source=pack, root=pack_root)
    return skills


def build_pack_resources(root):
    """A ``PackResources`` fed from every synthetic pack under ``root``."""
    resources = PackResources()
    for pack, include in PACK_INCLUDES.items():
        pack_root = root / pack
        if pack_root.is_dir():
            dirs = harvest.skill_dirs(pack_root, include)
            files = harvest.pack_files(pack_root, include, dirs)
            resources.add(pack, pack_root, files, dirs)
    return resources


def load_pack_prompts(root, pack):
    """The prompts harvested from one pack's own source root under ``root``."""
    pack_root = root / pack
    if not pack_root.is_dir():
        return []
    files = harvest.prompt_files(pack_root, PROMPT_INCLUDE)
    return load_prompts(files, pack=pack, source=pack)


def load_all_prompts(root):
    """Every pack's prompts under ``root``, combined."""
    prompts = []
    for pack in PACK_INCLUDES:
        prompts += load_pack_prompts(root, pack)
    return prompts


def _build_tree(root):
    """Write the synthetic skills tree into ``root`` and return it."""
    for name, desc in FLAT.items():
        _write(root / "flatsource" / name, name, desc)
    for plugin, skills in NESTED.items():
        for name, desc in skills.items():
            _write(root / "deepsource" / plugin / name, name, desc)
    # Pack-level files: outside every skill directory, so not skills themselves.
    (root / "deepsource" / "README.md").write_text("not a skill\n")
    shared = root / "deepsource" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "guide.md").write_text("shared guidance\n")
    (shared / "nested").mkdir(exist_ok=True)
    (shared / "nested" / "schema.json").write_text("{}\n")
    # A dotfile, which is the only pack-level "file" grafana actually ships.
    # It must not earn the pack an index row pointing at nothing readable.
    (root / "flatsource" / ".gitkeep").write_text("")
    return root


def _write(path, name, description):
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nBody.\n"
    )


def _build_prompts(root):
    """One prompt per pack, plus a file that is not in a pack folder at all."""
    _prompt(
        root / "flatsource" / "hello.md",
        "description: Say hello.\n"
        "arguments:\n"
        "- name: who\n"
        "  required: true\n"
        "- name: greeting\n"
        "  default: Hello\n",
        "{{ greeting }}, {{ who }}.\n",
    )
    # LogQL uses single braces, which is why placeholders are double ones.
    _prompt(
        root / "deepsource" / "check.md",
        "description: Check a service.\narguments:\n- name: service\n",
        '{app="{{ service }}"} |= "error"\n',
    )
    (root / "loose.md").write_text("---\ndescription: not in a pack\n---\nnope\n")
    return root


def _prompt(path, frontmatter, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}")


@pytest.fixture
def prompts_dir(tmp_path_factory):
    """A prompts root: ``<pack>/<name>.md``, for the packs ``skills_dir`` holds."""
    return _build_prompts(tmp_path_factory.mktemp("prompts"))


@pytest.fixture(scope="module")
def prompts_dir_module(tmp_path_factory):
    """Module-scoped twin of ``prompts_dir``, for the HTTP server fixture."""
    return _build_prompts(tmp_path_factory.mktemp("prompts"))


@pytest.fixture
def skills_dir(tmp_path):
    """A skills root holding one flat source and one two-level source.

    Mirrors the real sources: n8n is ``<source>/<skill>/`` while grafana is
    ``<source>/<plugin>/<skill>/``.
    """
    return _build_tree(tmp_path)


@pytest.fixture(scope="module")
def skills_dir_module(tmp_path_factory):
    """Module-scoped twin of ``skills_dir``.

    The HTTP tests start one uvicorn server per module, so the tree it serves
    has to outlive a single test.
    """
    return _build_tree(tmp_path_factory.mktemp("skills"))
