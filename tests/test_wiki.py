"""The wiki's reference pages are generated, so they can go stale like the spec.

Skipped entirely when the submodule is not checked out: a clone without it is a
working clone, and CI should not need the wiki to test the server.
"""

import pathlib
import re
import subprocess
import sys

import pytest

from kubed.mcp_kb.config import schema
from kubed.mcp_kb.main import build_parser
from kubed.mcp_kb.mcp import request

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).parent.parent
WIKI = REPO / "wiki"
GENERATOR = REPO / "scripts" / "generate_wiki.py"

needs_wiki = pytest.mark.skipif(
    not (WIKI / "Home.md").is_file(),
    reason="wiki submodule not checked out",
)

# The pages that are not prose: two navigation fragments GitHub renders around
# every other page, rather than pages a reader can be sent to.
FRAGMENTS = {"_Sidebar", "_Footer"}


def _pages() -> set[str]:
    return {p.stem for p in WIKI.glob("*.md")}


@needs_wiki
def test_the_generated_pages_are_current():
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@needs_wiki
def test_every_internal_link_resolves():
    """A broken wiki link is invisible until somebody clicks it."""
    pages = _pages()
    broken = []
    for page in sorted(WIKI.glob("*.md")):
        body = page.read_text()
        # Strip inline code first: a bracketed glob in a YAML example is not a link.
        body = re.sub(r"```.*?```", "", body, flags=re.S)
        body = re.sub(r"`[^`\n]*`", "", body)
        for text, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", body):
            if target.startswith(("http", "#", "mailto:")):
                continue
            if target.split("#")[0] not in pages:
                broken.append(f"{page.name}: [{text}]({target})")
    assert not broken, "broken wiki links: " + "; ".join(broken)


@needs_wiki
def test_no_page_is_shadowed_by_a_file_in_a_subdirectory():
    """A wiki page is addressed by basename, whatever directory it sits in.

    `wiki/notes/Tools.md` and `wiki/Tools.md` would therefore both answer to
    /wiki/Tools, and GitHub serves the fragment — so the page appears to have
    lost everything but its prose while the raw file is perfect. The notes live
    in wiki/notes/ and are suffixed `.notes.md` for exactly that reason; this
    keeps a bare `<page>.md` from coming back.
    """
    top = _pages()
    nested = {
        p: p.stem
        for p in WIKI.rglob("*.md")
        if p.parent != WIKI and ".git" not in p.parts
    }
    clashes = [str(p.relative_to(WIKI)) for p, stem in nested.items() if stem in top]
    assert not clashes, (
        "these files shadow a top-level wiki page by basename: " + ", ".join(clashes)
    )


@needs_wiki
def test_the_sidebar_lists_every_page():
    """The sidebar is the only navigation a GitHub wiki has.

    Asserted against the pages on disk rather than a list written out here: a
    page added and never linked is unreachable except by guessing its URL, and
    that is precisely the failure nobody notices.
    """
    sidebar = (WIKI / "_Sidebar.md").read_text()
    for page in sorted(_pages() - FRAGMENTS):
        assert f"({page})" in sidebar, f"{page} is missing from _Sidebar.md"


@needs_wiki
def test_configuration_documents_every_config_model():
    """A model the generator's own list forgot would silently go undocumented.

    `--check` compares only the pages the generator renders, so it cannot catch
    a section that was never rendered in the first place. Asserted against the
    live schema for that reason.
    """
    page = (WIKI / "Configuration.md").read_text()
    models = {"Config", *schema().get("$defs", {})}
    missing = [name for name in sorted(models) if f"\n## {name}\n" not in page]
    assert not missing, "no Configuration.md section for: " + ", ".join(missing)


@needs_wiki
def test_installing_documents_every_url_parameter_and_header():
    """The URL is the whole client-side API, and it is hand-written prose.

    A parameter added to `request.py` and left out of Installing is a feature
    that ships invisible to anyone connecting a client from the manual.
    """
    page = (WIKI / "Installing.md").read_text().lower()
    parameters = [
        request.LIBRARY_PARAM,
        request.TAGS_PARAM,
        request.PROMPTS_PARAM,
        request.RESOURCES_PARAM,
        request.LISTING_PARAM,
    ]
    headers = [
        request.LIBRARY_HEADER,
        request.PACK_HEADER,
        request.TAGS_HEADER,
        request.PROMPTS_HEADER,
        request.RESOURCES_HEADER,
        request.LISTING_HEADER,
    ]
    for name in parameters:
        assert f"?{name}=" in page, f"?{name}= is missing from Installing.md"
    for name in headers:
        assert name in page, f"{name} is missing from Installing.md"


@needs_wiki
def test_deployment_documents_every_environment_variable():
    """Read off the parser, which is the one place a variable is declared."""
    page = (WIKI / "Deployment.md").read_text()
    declared = set()
    for action in build_parser()._actions:
        declared |= set(re.findall(r"\(env: (\w+)\)", action.help or ""))
    assert declared, "no (env: …) fallbacks found — has main.py's help changed?"
    missing = [name for name in sorted(declared) if f"`{name}`" not in page]
    assert not missing, "no Deployment.md row for: " + ", ".join(missing)
