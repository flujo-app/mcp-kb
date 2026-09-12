"""Shared fixtures: a synthetic skills tree covering both nesting depths."""

import pytest

FLAT = {"alpha": "First skill.", "beta": "Second skill."}
NESTED = {"plugin-a": {"gamma": "Third skill."}, "plugin-b": {"delta": "Fourth skill."}}


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
