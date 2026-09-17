"""Shared fixtures: a synthetic skills tree covering both nesting depths."""

from pathlib import Path

import pytest

from kubed.mcp_kb.catalogue import harvest
from kubed.mcp_kb.catalogue.prompts import load_prompts
from kubed.mcp_kb.catalogue.skills import LibraryFiles, load_skills
from kubed.mcp_kb.config import Config
from kubed.mcp_kb.plugins import Fetch, Globs, Plugin

# The WebDAV server fixture lives in its own module -- it is a server, not a
# tree -- and is registered here so a test can ask for `webdav` by name.
from tests.webdav_server import webdav  # noqa: F401 - a fixture, used by name

FLAT = {"alpha": "First skill.", "beta": "Second skill."}
NESTED = {"plugin-a": {"gamma": "Third skill."}, "plugin-b": {"delta": "Fourth skill."}}

# Each synthetic library is one plugin whose root is that library's directory,
# so the globs are relative to it rather than to the tree root. "**/SKILL.md"
# finds a skill at any depth under a library -- flatsource lays its directly
# under itself, deepsource one level deeper under a folder -- and
# harvest.folder_of() (via SKILL_ROOTS) gives the nested ones their folder.
SKILLS_GLOBS = Globs(skills=("**/SKILL.md",), files=("**/*",))
LIBRARY_GLOBS = {"flatsource": SKILLS_GLOBS, "deepsource": SKILLS_GLOBS}
PROMPT_GLOBS = Globs(skills=(), prompts=("*.md",))

# What each synthetic skills plugin declares about itself. A tag per plugin, so
# a tag scope can tell the two apart the way a library scope can.
PLUGIN_TAGS = {"flatsource": ("flat",), "deepsource": ("deep",)}


def fetch_key(path: Path) -> str:
    """The fetch key a ``file://`` plugin at ``path`` gets: what ``refresh`` returns."""
    return f"file://{path}"


def key_of(config: Config, plugin: str) -> str:
    """The fetch key of the named plugin in ``config``, whatever scheme it uses."""
    return next(p.address.key for p in config.plugins if p.name == plugin)


def fake_plugin(name: str, root: Path, **overrides) -> Plugin:
    """A resolved plugin over a local directory, for tests below the server.

    The id is the name and the fetch is the directory itself, which is what
    ``make_config`` below produces once it has been through the config.
    """
    fields = {
        "id": name,
        "name": name,
        "description": "",
        "category": None,
        "tags": PLUGIN_TAGS.get(name, ()),
        "keywords": (),
        "version": None,
        "fetch": Fetch(key=fetch_key(root), backend="file", url=str(root)),
        "subdir": "",
        "globs": Globs(),
    }
    fields.update(overrides)
    return Plugin(**fields)


def load_all_skills(root):
    """Harvest and load every synthetic library under ``root``, as one catalogue."""
    skills = []
    for library, globs in LIBRARY_GLOBS.items():
        library_root = root / library
        if library_root.is_dir():
            dirs = harvest.skill_dirs(library_root, globs)
            skills += load_skills(
                dirs,
                library=library,
                plugin=fake_plugin(library, library_root),
                root=library_root,
            )
    return skills


def build_library_files(root):
    """A ``LibraryFiles`` fed from every synthetic library under ``root``."""
    resources = LibraryFiles()
    for library, globs in LIBRARY_GLOBS.items():
        library_root = root / library
        if library_root.is_dir():
            dirs = harvest.skill_dirs(library_root, globs)
            files = harvest.library_files(library_root, globs, dirs)
            resources.add(
                library,
                library_root,
                files,
                dirs,
                plugin=fake_plugin(library, library_root),
            )
    return resources


def load_library_prompts(root, library):
    """The prompts harvested from one library's own prompt root under ``root``."""
    library_root = root / library
    if not library_root.is_dir():
        return []
    files = harvest.prompt_files(library_root, PROMPT_GLOBS)
    return load_prompts(files, library=library, plugin=f"{library}-prompts")


def load_all_prompts(root):
    """Every library's prompts under ``root``, combined."""
    prompts = []
    for library in LIBRARY_GLOBS:
        prompts += load_library_prompts(root, library)
    return prompts


def make_config(skills_dir=None, prompts_dir=None, libraries=None, refresh=None):
    """A ``Config`` whose ``file://`` plugins mirror the fixture trees above.

    One plugin per top-level directory under ``skills_dir`` and one
    ``<name>-prompts`` plugin per top-level directory under ``prompts_dir``,
    and one library per name listing both -- the two trees are separate roots
    and a plugin is exactly one root. ``libraries`` narrows to the named
    libraries: a deployment that should serve less gets a config that lists
    less.

    ``refresh`` puts the plugins behind a declared ``local`` source carrying
    that interval instead of the built-in ``file://`` scheme, which carries
    none -- the shape a test of the refresh loop needs.
    """
    plugins = []
    members: dict[str, list[str]] = {}
    for base, is_prompts in ((skills_dir, False), (prompts_dir, True)):
        if base is None:
            continue
        for name in sorted(p.name for p in base.iterdir() if p.is_dir()):
            if libraries is not None and name not in libraries:
                continue
            path = base / name
            source = f"file://{path}" if refresh is None else f"local://{path.relative_to('/')}"
            if is_prompts:
                plugin = {
                    "name": f"{name}-prompts",
                    "source": source,
                    "skills": [],
                    "prompts": ["*.md"],
                }
            else:
                plugin = {
                    "name": name,
                    "source": source,
                    "tags": list(PLUGIN_TAGS.get(name, ())),
                    "skills": ["**/SKILL.md"],
                    "files": ["**/*"],
                }
            plugins.append(plugin)
            members.setdefault(name, []).append(plugin["name"])
    sources = [] if refresh is None else [
        {"name": "local", "url": "file:///", "refresh": refresh}
    ]
    return Config.model_validate(
        {
            "sources": sources,
            "plugins": plugins,
            "libraries": [
                {"name": name, "plugins": names} for name, names in members.items()
            ],
        }
    )


def _build_tree(root):
    """Write the synthetic skills tree into ``root`` and return it."""
    for name, desc in FLAT.items():
        _write(root / "flatsource" / name, name, desc)
    for plugin, skills in NESTED.items():
        for name, desc in skills.items():
            _write(root / "deepsource" / plugin / name, name, desc)
    # Library-level files: outside every skill directory, so not skills themselves.
    (root / "deepsource" / "README.md").write_text("not a skill\n")
    shared = root / "deepsource" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "guide.md").write_text("shared guidance\n")
    (shared / "nested").mkdir(exist_ok=True)
    (shared / "nested" / "schema.json").write_text("{}\n")
    # A dotfile, which is the only library-level "file" grafana actually ships.
    # It must not earn the library an index row pointing at nothing readable.
    (root / "flatsource" / ".gitkeep").write_text("")
    return root


def _write(path, name, description):
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nBody.\n"
    )


def _build_prompts(root):
    """One prompt per library, plus a file that is not in a library folder at all."""
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
    (root / "loose.md").write_text("---\ndescription: not in a library\n---\nnope\n")
    return root


def _prompt(path, frontmatter, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}")


@pytest.fixture
def prompts_dir(tmp_path_factory):
    """A prompts root: ``<library>/<name>.md``, for the libraries in ``skills_dir``."""
    return _build_prompts(tmp_path_factory.mktemp("prompts"))


@pytest.fixture(scope="module")
def prompts_dir_module(tmp_path_factory):
    """Module-scoped twin of ``prompts_dir``, for the HTTP server fixture."""
    return _build_prompts(tmp_path_factory.mktemp("prompts"))


@pytest.fixture
def skills_dir(tmp_path):
    """A skills root holding one flat plugin and one two-level plugin.

    Mirrors the real libraries: n8n is ``<root>/<skill>/`` while grafana is
    ``<root>/<folder>/<skill>/``.
    """
    return _build_tree(tmp_path)


@pytest.fixture(scope="module")
def skills_dir_module(tmp_path_factory):
    """Module-scoped twin of ``skills_dir``.

    The HTTP tests start one uvicorn server per module, so the tree it serves
    has to outlive a single test.
    """
    return _build_tree(tmp_path_factory.mktemp("skills"))
