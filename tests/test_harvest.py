"""Turning a source's include globs into skill dirs, prompt files and pack files."""

from pathlib import Path

import pytest

from kubed.mcp_kb.catalogue.harvest import (
    group_of,
    pack_files,
    patterns,
    prompt_files,
    skill_dirs,
)
from kubed.mcp_kb.config import Include


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
    # config.py's Include validator already refuses "../**" at construction;
    # model_construct bypasses it so this test exercises harvest.files()'s own
    # defence-in-depth guard directly, independent of that outer validation.
    (tmp_path.parent / "outside.md").write_text("no\n")
    include = Include.model_construct(files=["../**"])
    assert pack_files(tree, include, []) == []


def test_the_group_is_the_containing_directory_unless_that_is_a_skill_root(tree):
    assert group_of(tree / "skills/grafana-lgtm/loki", tree) == "grafana-lgtm"
    assert group_of(tree / "skills/flat", tree) is None
    assert group_of(tree / ".github/skills/gh", tree) is None
    assert group_of(tree / "template", tree) is None


@pytest.mark.unit
def test_a_trailing_globstar_means_everything_underneath(tmp_path):
    """`dir/**` and `dir/**/*` must find the same files on every version.

    Before Python 3.13 a trailing `**` matches directories only, so this test
    reads as a tautology on 3.13+ and as the real thing on 3.11 and 3.12 --
    where, before `patterns` normalised it, the first form found nothing.
    """
    tree = tmp_path / "pack"
    (tree / "shared" / "deep").mkdir(parents=True)
    (tree / "shared" / "tokens.md").write_text("t")
    (tree / "shared" / "deep" / "more.md").write_text("m")

    assert pack_files(tree, Include(files=["shared/**"]), []) == pack_files(
        tree, Include(files=["shared/**/*"]), []
    )
    assert pack_files(tree, Include(files=["shared/**"]), []) == [
        "shared/deep/more.md",
        "shared/tokens.md",
    ]


@pytest.mark.unit
def test_a_trailing_globstar_is_normalised_before_it_reaches_glob():
    """The version-independent half of the rule above.

    `pathlib.glob` is what changed in 3.13, so a test that calls it can only
    assert the old behaviour on an old interpreter. This one pins the
    normalisation itself, and fails everywhere if it is dropped.
    """
    assert patterns("files", Include(files=["shared/**"])) == ("shared/**/*",)
    assert patterns("files", Include(files=["shared/**/*"])) == ("shared/**/*",)
    assert patterns("files", Include(files=["shared/*"])) == ("shared/*",)
    assert patterns("skills", Include(skills=["skills/*/SKILL.md"])) == (
        "skills/*/SKILL.md",
    )


@pytest.mark.unit
def test_a_configmap_style_symlink_farm_is_harvested(tmp_path):
    """A Kubernetes ConfigMap mount is entirely symlinks through a dot-directory.

    Every key is `key -> ..data/key` and `..data -> ..<timestamp>/`, so judging
    hidden-ness by the RESOLVED path throws the whole mount away — which made
    `file://`, the backend that exists to serve mounted prompts, serve nothing
    at all. Containment is the security property; the target's name is not.
    """
    root = tmp_path / "prompts"
    data = root / "..2026_09_16_13_15_49.612400444"
    data.mkdir(parents=True)
    (data / "debug-logs.md").write_text("---\ndescription: x\n---\nbody")
    (root / "..data").symlink_to(data)
    (root / "debug-logs.md").symlink_to(root / "..data" / "debug-logs.md")

    found = prompt_files(root, Include(skills=[], prompts=["*.md"]))

    assert [p.name for p in found] == ["debug-logs.md"]
    assert found[0].read_text().endswith("body")


@pytest.mark.unit
def test_a_symlink_out_of_the_root_is_still_refused(tmp_path):
    """The other half: judging the requested path must not weaken containment."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.md").write_text("---\ndescription: x\n---\nno")
    root = tmp_path / "prompts"
    root.mkdir()
    (root / "secret.md").symlink_to(outside / "secret.md")

    assert prompt_files(root, Include(skills=[], prompts=["*.md"])) == []


@pytest.mark.unit
def test_only_a_whole_trailing_component_is_a_globstar():
    """`logs**` is not a globstar: it matches names beginning with "logs".

    Rewriting it to `logs**/*` would silently change it to mean the descendants
    of those names instead — a different set, and empty for a plain file.
    """
    assert patterns("files", Include(files=["logs**"])) == ("logs**",)
    assert patterns("files", Include(files=["a/logs**"])) == ("a/logs**",)
    assert patterns("files", Include(files=["**"])) == ("**/*",)
    assert patterns("files", Include(files=["a/**"])) == ("a/**/*",)


@pytest.mark.unit
def test_a_skills_glob_may_name_the_directory(tmp_path):
    """`skills/*` and `skills/*/SKILL.md` select the same skills.

    A folder holding a SKILL.md *is* the skill, so naming the folder is the
    spelling most people reach for — and it used to match nothing, silently.
    """
    root = tmp_path / "src"
    for name in ("alpha", "beta"):
        d = root / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody")

    by_dir = skill_dirs(root, Include(skills=["skills/*"]))
    by_file = skill_dirs(root, Include(skills=["skills/*/SKILL.md"]))

    assert by_dir == by_file
    assert [p.name for p in by_dir] == ["alpha", "beta"]


@pytest.mark.unit
def test_a_composite_folder_registers_every_skill_beneath_it(tmp_path):
    """Naming a folder of folders registers the set, not nothing.

    This is how a grouped pack reads: `skills/grafana-lgtm` is not itself a
    skill, it holds them, and pointing at it should mean all of them.
    """
    root = tmp_path / "src"
    for group, name in (("lgtm", "loki"), ("lgtm", "tempo"), ("sdk", "plugins")):
        d = root / "skills" / group / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody")

    assert [p.name for p in skill_dirs(root, Include(skills=["skills/lgtm"]))] == [
        "loki",
        "tempo",
    ]
    # sorted by path, so the lgtm pair precedes sdk/plugins
    assert [p.name for p in skill_dirs(root, Include(skills=["skills"]))] == [
        "loki",
        "tempo",
        "plugins",
    ]


@pytest.mark.unit
def test_a_directory_glob_does_not_reach_outside_the_root(tmp_path):
    """The directory spelling must not become a way around containment."""
    outside = tmp_path / "elsewhere" / "secret"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\nname: secret\n---\nno")
    root = tmp_path / "src"
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "linked").symlink_to(outside)

    assert skill_dirs(root, Include(skills=["skills/*"])) == []


@pytest.mark.unit
def test_a_symlink_loop_fails_that_link_not_the_harvest(tmp_path, monkeypatch):
    """Before 3.13 `resolve()` raises RuntimeError on a loop, not OSError.

    Forced here so the test means the same thing on every interpreter.
    """
    root = tmp_path / "src"
    good = root / "skills" / "good"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text("---\nname: good\n---\nbody")
    (root / "skills" / "loop").symlink_to(root / "skills" / "loop")

    real = Path.resolve

    def resolve(self, strict=False):
        if self.name == "loop":
            raise RuntimeError(f"Symlink loop from {self}")
        return real(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert [p.name for p in skill_dirs(root, Include(skills=["skills/*"]))] == ["good"]


@pytest.mark.unit
def test_a_skill_md_symlinked_out_of_the_root_does_not_register(tmp_path):
    """A directory glob must contain its SKILL.md hits the way a file glob does."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: leak\n---\nno")
    root = tmp_path / "src"
    leak = root / "skills" / "leak"
    leak.mkdir(parents=True)
    (leak / "SKILL.md").symlink_to(outside / "SKILL.md")

    assert skill_dirs(root, Include(skills=["skills"])) == []


@pytest.mark.unit
def test_a_link_back_to_an_ancestor_does_not_loop_a_directory_glob(tmp_path):
    """A directory glob walks with `rglob`, which does not follow directory links.

    Checked on 3.11.16, 3.12.14 and 3.13.5: a link back up the tree yields the one
    real skill and ends. Pinned here because CI runs the old end of the range on
    every pull request, and a switch to a link-following walk would hang instead.
    """
    skills = tmp_path / "src" / "skills"
    (skills / "a").mkdir(parents=True)
    (skills / "a" / "SKILL.md").write_text("---\nname: a\n---\nbody")
    (skills / "a" / "up").symlink_to(skills)

    found = skill_dirs(tmp_path / "src", Include(skills=["skills"]))

    assert [p.name for p in found] == ["a"]
