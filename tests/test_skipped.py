"""What one source ships but cannot serve: skipped, and named in ``/health``.

A source whose tree holds something the address space has no room for keeps
serving everything else. The one thing is left out and listed under
``skipped`` in that source's ``/health`` entry, with the reason, so it is
visible rather than silently shadowed:

- a library file whose address lies inside one of the source's own skills,
  or that is named like the server's own indexes;

Across two sources the rule is the older one in ``test_conflicts.py``: the
later source fails whole.
"""

import httpx
import pytest
from fastmcp import Client

from kubed.mcp_kb import KnowledgeBase
from kubed.mcp_kb.config import Config
from kubed.mcp_kb.mcp.scope import Scope

pytestmark = pytest.mark.unit


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _skill(root, rel, name, description):
    _write(root, f"{rel}/SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\n")


def _kb(tmp_path, build, include):
    root = tmp_path / "src"
    root.mkdir()
    build(root)
    source = {"name": "src", "library": "lib", "url": f"file://{root}", "include": include}
    return KnowledgeBase(Config.model_validate({"sources": [source]}), tmp_path / "c")


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
    return {row["path"]: row["reason"] for row in health["sources"]["src"]["skipped"]}


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


OVERLAP_INCLUDE = {"files": ["shared/**", "grafana-lgtm/**", "docs/**", "_index.md"]}


async def test_a_library_file_inside_one_of_its_own_sources_skills_is_skipped(tmp_path):
    kb = _kb(tmp_path, _overlapping, OVERLAP_INCLUDE)
    health = await _health(kb)

    entry = health["sources"]["src"]
    assert entry["status"] == "ok"
    assert entry["files"] == 1
    skipped = _skipped(health)
    assert "skill://lib/grafana-lgtm/loki" in skipped["grafana-lgtm/loki/notes.md"]
    assert "skill://lib/grafana-lgtm/tempo" in skipped["grafana-lgtm/tempo/SKILL.md"]

    files = await _read(kb, "skill://lib/_files.md")
    assert "skill://lib/shared/ok.md" in files
    assert "grafana-lgtm" not in files
    assert await _read(kb, "skill://lib/grafana-lgtm/loki/notes.md") is None
    assert "Query traces." in await _read(kb, "skill://lib/grafana-lgtm/tempo/SKILL.md")
    # One URI, one answer: a scope that hides the skill does not uncover the file.
    pinned = Scope.parse("lib/grafana-k6")
    assert kb.catalogue.read("skill://lib/grafana-lgtm/tempo/SKILL.md", pinned) is None


async def test_a_library_file_named_like_an_index_is_skipped(tmp_path):
    kb = _kb(tmp_path, _overlapping, OVERLAP_INCLUDE)
    health = await _health(kb)

    skipped = _skipped(health)
    assert "_index.md" in skipped["_index.md"]
    assert "_files.md" in skipped["docs/_files.md"]
    assert "LIBRARY INDEX" not in await _read(kb, "skill://lib/_index.md")
    assert await _read(kb, "skill://lib/docs/_files.md") is None
    assert "_index.md" not in await _read(kb, "skill://lib/_files.md")


async def test_a_source_with_nothing_skipped_reports_no_skipped_list(tmp_path):
    kb = _kb(
        tmp_path,
        lambda r: (_skill(r, "skills/a", "a", "A."), _write(r, "shared/x.md", "x\n")),
        {"files": ["shared/**"]},
    )
    assert "skipped" not in (await _health(kb))["sources"]["src"]
