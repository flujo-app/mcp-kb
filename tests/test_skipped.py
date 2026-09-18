"""What one plugin ships but cannot serve: skipped, and named in ``/health``.

A plugin whose tree holds something the address space has no room for keeps
serving everything else. The one thing is left out and listed under
``skipped`` in that plugin's ``/health`` entry, with the reason, so it is
visible rather than silently shadowed:

- a library file whose address lies inside one of the plugin's own skills,
  or that is named like the server's own indexes;
- a skill whose frontmatter ``name`` is not one valid segment matching its
  directory, or whose address a skill before it already has;
- a prompt whose name a prompt before it already has.

Across two plugins of one library the rule is the one in ``test_conflicts.py``:
the later plugin fails in that library.
"""

import httpx
import pytest
from fastmcp import Client

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.catalogue import skills
from kubed.mcp_kb.config import Config
from kubed.mcp_kb.mcp.scope import Scope

pytestmark = pytest.mark.unit


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _skill(root, rel, name, description):
    _write(
        root, f"{rel}/SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\n"
    )


def _kb(tmp_path, build, globs):
    root = tmp_path / "src"
    root.mkdir()
    build(root)
    config = Config.model_validate(
        {
            "plugins": [{"name": "src", "source": f"file://{root}", **globs}],
            "libraries": [{"name": "lib", "plugins": ["src"]}],
        }
    )
    return KnowledgeBase(config, tmp_path / "c")


async def _health(knowledge_base):
    transport = httpx.ASGITransport(app=knowledge_base.mcp.http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as http:
        return (await http.get("/health")).json()


async def _read(knowledge_base, uri):
    """The body at ``uri``, or None when the server says it is not there."""
    async with Client(knowledge_base.mcp) as client:
        try:
            return (await client.read_resource(uri))[0].text
        except Exception as exc:  # noqa: BLE001 - "not found" is the answer
            assert "not found" in str(exc)
            return None


def _skipped(health):
    return {row["path"]: row["reason"] for row in health["plugins"]["src"]["skipped"]}


# -- library files --------------------------------------------------------------


def _overlapping(root):
    _skill(root, "skills/grafana-lgtm/loki", "loki", "Query logs.")
    _skill(root, "skills/grafana-lgtm/tempo", "tempo", "Query traces.")
    _skill(root, "skills/grafana-k6/k6", "k6", "Load test.")
    _write(root, "grafana-lgtm/loki/notes.md", "LIBRARY NOTES\n")
    _write(root, "grafana-lgtm/tempo/SKILL.md", "LIBRARY FILE SHADOWED\n")
    _write(root, "_index.md", "LIBRARY INDEX\n")
    _write(root, "docs/_files.md", "LIBRARY FILES LIST\n")
    _write(root, "shared/ok.md", "fine\n")


OVERLAP_GLOBS = {"files": ["shared/**", "grafana-lgtm/**", "docs/**", "_index.md"]}


async def test_a_library_file_inside_one_of_its_own_plugins_skills_is_skipped(tmp_path):
    kb = _kb(tmp_path, _overlapping, OVERLAP_GLOBS)
    health = await _health(kb)

    entry = health["plugins"]["src"]
    assert entry["status"] == "ok"
    assert entry["files"] == 1
    assert health["libraries"]["lib"]["files"] == 1
    skipped = _skipped(health)
    assert "grafana-lgtm/loki" in skipped["grafana-lgtm/loki/notes.md"]
    assert "grafana-lgtm/tempo" in skipped["grafana-lgtm/tempo/SKILL.md"]

    files = await _read(kb, "skill://lib/_files.md")
    assert "skill://lib/shared/ok.md" in files
    assert "grafana-lgtm" not in files
    assert await _read(kb, "skill://lib/grafana-lgtm/loki/notes.md") is None
    assert "Query traces." in await _read(kb, "skill://lib/grafana-lgtm/tempo/SKILL.md")
    # One URI, one answer: a scope that hides the skill does not uncover the file.
    pinned = Scope.parse("lib", categories=["nope"])
    assert kb.catalogue.read("skill://lib/grafana-lgtm/tempo/SKILL.md", pinned) is None


async def test_a_library_file_named_like_an_index_is_skipped(tmp_path):
    kb = _kb(tmp_path, _overlapping, OVERLAP_GLOBS)
    health = await _health(kb)

    skipped = _skipped(health)
    assert "_index.md" in skipped["_index.md"]
    assert "_files.md" in skipped["docs/_files.md"]
    assert "LIBRARY INDEX" not in await _read(kb, "skill://lib/_index.md")
    assert await _read(kb, "skill://lib/docs/_files.md") is None
    assert "_index.md" not in await _read(kb, "skill://lib/_files.md")


async def test_a_plugin_with_nothing_skipped_reports_no_skipped_list(tmp_path):
    kb = _kb(
        tmp_path,
        lambda r: (_skill(r, "skills/a", "a", "A."), _write(r, "shared/x.md", "x\n")),
        {"files": ["shared/**"]},
    )
    assert "skipped" not in (await _health(kb))["plugins"]["src"]


# -- skills -----------------------------------------------------------------------


def _misnamed(root):
    _skill(root, "skills/f/good", "good", "Good.")
    _skill(root, "skills/f/dotdot", "..", "Dots.")
    _skill(root, "skills/f/slash", "grafana-lgtm/loki", "Slash.")
    _skill(root, "skills/f/Upper", "Upper", "Upper.")
    _skill(root, "skills/f/mismatch", "other", "Mismatch.")
    _skill(root, "skills/f/idx", "_index.md", "Index.")
    # One address reached from two skill roots: the first found serves.
    _skill(root, ".claude/skills/dup", "dup", "First dup.")
    _skill(root, "skills/dup", "dup", "Second dup.")


async def test_a_skill_that_cannot_be_one_address_segment_is_skipped(tmp_path):
    kb = _kb(tmp_path, _misnamed, {})
    health = await _health(kb)

    entry = health["plugins"]["src"]
    assert entry["status"] == "ok"
    assert entry["skills"] == 2
    skipped = _skipped(health)
    for path in ("skills/f/dotdot", "skills/f/slash", "skills/f/Upper", "skills/f/idx"):
        assert "naming rule" in skipped[path], path
    assert "directory" in skipped["skills/f/mismatch"]
    assert skipped["skills/dup"] == "dup is already served by .claude/skills/dup"
    assert set(skipped) == {
        "skills/f/dotdot",
        "skills/f/slash",
        "skills/f/Upper",
        "skills/f/mismatch",
        "skills/f/idx",
        "skills/dup",
    }

    folder = await _read(kb, "skill://lib/f/_index.md")
    assert folder.startswith("# lib/f — 1 skill\n")
    assert "skill://lib/f/good/SKILL.md" in folder
    assert ".." not in folder and "grafana-lgtm" not in folder
    library = await _read(kb, "skill://lib/_index.md")
    assert library.startswith("# lib — 2 skills\n")
    assert library.count("skill://lib/dup/SKILL.md") == 1
    assert "First dup." in await _read(kb, "skill://lib/dup/SKILL.md")
    for uri in (
        "skill://lib/f/other/SKILL.md",
        "skill://lib/f/mismatch/SKILL.md",
        "skill://lib/f/Upper/SKILL.md",
        "skill://lib/f/grafana-lgtm/loki/SKILL.md",
        "skill://lib/f/_index.md/SKILL.md",
    ):
        assert await _read(kb, uri) is None, uri


async def test_a_plugin_that_is_one_skill_need_not_match_its_cache_directory(tmp_path):
    """A git fetch's root is a directory named for a commit, not for the skill."""
    kb = _kb(
        tmp_path,
        lambda r: _skill(r, ".", "whole", "The whole plugin."),
        {"skills": ["SKILL.md"]},
    )
    health = await _health(kb)

    assert health["plugins"]["src"]["skills"] == 1
    assert "skipped" not in health["plugins"]["src"]
    assert "The whole plugin." in await _read(kb, "skill://lib/whole/SKILL.md")


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("a", True),
        ("pdf-processing", True),
        ("k6", True),
        ("x" * 64, True),
        ("x" * 65, False),
        ("-a", False),
        ("a-", False),
        ("a--b", False),
        ("a_b", False),
        ("a.b", False),
        ("A", False),
        ("..", False),
        ("a/b", False),
        ("", False),
    ],
)
def test_the_agent_skills_naming_rule(name, valid):
    assert (skills.naming_problem(name) is None) is valid


# -- prompts ----------------------------------------------------------------------


async def test_a_second_prompt_with_one_name_in_a_plugin_is_skipped(tmp_path):
    def build(root):
        _write(root, "prompts/debug.md", "---\ndescription: 1\n---\nfirst\n")
        _write(root, "prompts/sub/debug.md", "---\ndescription: 2\n---\nsecond\n")

    kb = _kb(tmp_path, build, {})
    health = await _health(kb)

    assert health["plugins"]["src"]["prompts"] == 1
    assert _skipped(health)["prompts/sub/debug.md"] == (
        "prompt debug is already served by prompts/debug.md"
    )
    async with Client(kb.mcp) as client:
        prompts = await client.list_prompts()
        rendered = await client.get_prompt("lib_debug")
        mirrored = await client.call_tool("list_prompts", {})
    assert [p.name for p in prompts] == ["lib_debug"]
    assert "first" in rendered.messages[0].content.text
    assert mirrored.content[0].text.count("lib_debug") == 1


async def test_a_skipped_skills_own_files_neither_serve_nor_fail_the_plugin(tmp_path):
    """A `files:` glob that reaches into a skill the plugin then skips.

    Skill directories and library files come from one harvest, and library files
    never include anything inside a harvested skill — valid or not — so the
    skipped skill's files are simply not served. The plugin stays `ok`, lists the
    skill under `skipped`, and serves its valid skill.
    """
    root = tmp_path / "src"
    (root / "skills" / "good").mkdir(parents=True)
    (root / "skills" / "good" / "SKILL.md").write_text(
        "---\nname: good\ndescription: g\n---\nbody\n"
    )
    (root / "skills" / "bad").mkdir(parents=True)
    (root / "skills" / "bad" / "SKILL.md").write_text(
        "---\nname: Not_Valid\ndescription: b\n---\nbody\n"
    )
    (root / "skills" / "bad" / "notes.md").write_text("never served\n")
    config = Config.model_validate(
        {
            "plugins": [
                {"name": "lib", "source": f"file://{root}", "files": ["skills/bad/**/*"]}
            ],
            "libraries": [{"name": "lib", "plugins": ["lib"]}],
        }
    )
    kb = KnowledgeBase(config, tmp_path / "cache")
    status = kb.snapshot.status["plugins"]["lib"]

    assert status["status"] == "ok"
    assert [entry["path"] for entry in status["skipped"]] == ["skills/bad"]
    async with Client(kb.mcp) as client:
        assert "body" in (await client.read_resource("skill://lib/good/SKILL.md"))[0].text
        with pytest.raises(Exception, match="not found"):
            await client.read_resource("skill://lib/skills/bad/notes.md")
