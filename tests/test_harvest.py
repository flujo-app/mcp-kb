from pathlib import Path

import pytest

from mcp_school.config import Include
from mcp_school.harvest import group_of, pack_files, prompt_files, skill_dirs


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel, body in {
        "skills/grafana-lgtm/loki/SKILL.md": "---\nname: loki\ndescription: d\n---\n",
        "skills/flat/SKILL.md": "---\nname: flat\ndescription: d\n---\n",
        "template/SKILL.md": "---\nname: template\ndescription: d\n---\n",
        ".github/skills/gh/SKILL.md": "---\nname: gh\ndescription: d\n---\n",
        ".github/prompts/review.prompt.md": "---\ndescription: r\n---\nbody\n",
        "prompts/grafana/debug-logs.md": "---\ndescription: p\n---\nbody\n",
        "shared/tokens.md": "shared\n",
        "skills/flat/reference.md": "inside a skill\n",
        ".git/SKILL.md": "---\nname: git\ndescription: never\n---\n",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp_path


def rel(root: Path, paths) -> set[str]:
    return {Path(p).relative_to(root).as_posix() for p in paths}


def test_conventions_find_nested_skills_and_never_a_root_template(tree):
    assert rel(tree, skill_dirs(tree, Include())) == {
        "skills/grafana-lgtm/loki",
        "skills/flat",
        ".github/skills/gh",
    }


def test_a_dot_directory_is_skipped_unless_it_is_a_convention(tree):
    found = rel(tree, skill_dirs(tree, Include(skills=["**/SKILL.md"])))
    assert ".github/skills/gh" in found
    assert ".git" not in found


def test_setting_a_kind_replaces_its_defaults(tree):
    assert rel(tree, skill_dirs(tree, Include(skills=["template/SKILL.md"]))) == {
        "template"
    }


def test_an_empty_list_turns_a_kind_off(tree):
    assert prompt_files(tree, Include(prompts=[])) == []


def test_conventions_find_both_prompt_layouts(tree):
    assert rel(tree, prompt_files(tree, Include())) == {
        ".github/prompts/review.prompt.md",
        "prompts/grafana/debug-logs.md",
    }


def test_files_are_served_only_when_asked_for(tree):
    dirs = skill_dirs(tree, Include())
    assert pack_files(tree, Include(), dirs) == []
    assert pack_files(tree, Include(files=["shared/**"]), dirs) == ["shared/tokens.md"]


def test_a_file_inside_a_skill_is_never_a_pack_file(tree):
    # pack_files only excludes files inside a skill dir (per its docstring); a
    # broad "**/*.md" also legitimately matches the fixture's prompt files,
    # which live outside every skill dir returned by skill_dirs().
    dirs = skill_dirs(tree, Include())
    assert pack_files(tree, Include(files=["**/*.md"]), dirs) == [
        ".github/prompts/review.prompt.md",
        "prompts/grafana/debug-logs.md",
        "shared/tokens.md",
        "template/SKILL.md",
    ]


def test_a_glob_cannot_escape_the_source(tree, tmp_path):
    (tmp_path.parent / "outside.md").write_text("no\n")
    assert pack_files(tree, Include(files=["../**"]), []) == []


def test_the_group_is_the_containing_directory_unless_that_is_a_skill_root(tree):
    assert group_of(tree / "skills/grafana-lgtm/loki", tree) == "grafana-lgtm"
    assert group_of(tree / "skills/flat", tree) is None
    assert group_of(tree / ".github/skills/gh", tree) is None
    assert group_of(tree / "template", tree) is None
