"""``catalogue/`` and ``plugins/`` import no FastMCP -- proved with it blocked.

``AGENTS.md`` promises these are the FastMCP-free layers: ``plugins/`` resolves
what the config declares and what a marketplace publishes, ``catalogue/`` turns
that into what a client is served, and neither reaches for the protocol library
itself. The promise is worth nothing unproved, so this test blocks ``fastmcp``
in a fresh subprocess and imports every module of both directly -- found by
walking the packages, so a module added later is covered without being listed.

``kubed.mcp_kb``'s own ``__init__.py`` imports ``.server``, which imports
``fastmcp`` -- by design, the server needs it, and that is not what is under
test here. A bare stub stands in for the ``kubed.mcp_kb`` package so its
submodules resolve without running that ``__init__.py``, keeping the blocked
import limited to what ``catalogue/`` itself pulls in.
"""

import pkgutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).parent.parent

# Walked on disk, not imported: importing the package here would run the very
# imports the subprocess exists to block.
CATALOGUE_MODULES = tuple(
    f"kubed.mcp_kb.catalogue.{module.name}"
    for module in pkgutil.iter_modules([str(REPO / "kubed" / "mcp_kb" / "catalogue")])
)

PLUGINS_MODULES = (
    "kubed.mcp_kb.plugins",
    *(
        f"kubed.mcp_kb.plugins.{module.name}"
        for module in pkgutil.iter_modules(
            [str(REPO / "kubed" / "mcp_kb" / "plugins")]
        )
    ),
)

SCRIPT = textwrap.dedent(
    """
    import sys
    import types

    sys.path.insert(0, {repo!r})

    class _NoFastMCP:
        \"\"\"Raises the instant anything below tries to import fastmcp.\"\"\"

        def find_spec(self, name, path=None, target=None):
            if name == "fastmcp" or name.startswith("fastmcp."):
                raise ImportError(f"fastmcp import blocked: {{name}}")
            return None

    import kubed  # the namespace package: no __init__.py, nothing to run

    stub = types.ModuleType("kubed.mcp_kb")
    stub.__path__ = [{mcp_kb!r}]
    sys.modules["kubed.mcp_kb"] = stub

    sys.meta_path.insert(0, _NoFastMCP())

    for name in {modules!r}:
        __import__(name)

    print("ok")
    """
)


def imports_clean(modules: tuple[str, ...]) -> subprocess.CompletedProcess:
    """Import every module named, in a subprocess where fastmcp cannot load."""
    script = SCRIPT.format(
        repo=str(REPO), mcp_kb=str(REPO / "kubed" / "mcp_kb"), modules=modules
    )
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )


def test_every_catalogue_module_is_checked():
    """The walk finds the modules, the refresh loop among them."""
    assert "kubed.mcp_kb.catalogue.refresh" in CATALOGUE_MODULES
    assert len(CATALOGUE_MODULES) >= 7


def test_every_plugins_module_is_checked():
    """The walk finds the resolution layer, the marketplace reader among it."""
    assert "kubed.mcp_kb.plugins.marketplace" in PLUGINS_MODULES
    assert "kubed.mcp_kb.plugins" in PLUGINS_MODULES
    assert len(PLUGINS_MODULES) >= 5


def test_catalogue_imports_no_fastmcp():
    """Every catalogue module imports clean with fastmcp unreachable.

    Proved non-vacuous against the pre-split tree, where ``catalogue/index.py``
    and ``catalogue/snapshot.py`` imported ``FilePrompt`` from
    ``mcp/prompts.py`` -- which imports ``fastmcp`` itself -- and this test
    failed with exactly the blocked-import error.
    """
    result = imports_clean(CATALOGUE_MODULES)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_plugins_import_no_fastmcp():
    """And every module of the resolution layer, which is checked on its own.

    Two promises, two tests: ``plugins/`` is reachable from ``catalogue/`` but
    not the reverse, and a layer that has to wait for the other one to be
    fixed before its own boundary can be proved is not a boundary.
    """
    result = imports_clean(PLUGINS_MODULES)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
