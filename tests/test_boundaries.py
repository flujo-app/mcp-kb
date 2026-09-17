"""``catalogue/`` imports no FastMCP -- proved with the real thing blocked.

``AGENTS.md`` promises ``catalogue/`` is the FastMCP-free layer: everything a
client-facing concept needs before it becomes a served resource, tool or
prompt, with nothing here reaching for the protocol library itself. The
promise is worth nothing unimport, so this test blocks ``fastmcp`` in a fresh
subprocess and imports every ``catalogue`` module directly.

``kubed.mcp_kb``'s own ``__init__.py`` imports ``.server``, which imports
``fastmcp`` -- by design, the server needs it, and that is not what is under
test here. A bare stub stands in for the ``kubed.mcp_kb`` package so its
submodules resolve without running that ``__init__.py``, keeping the blocked
import limited to what ``catalogue/`` itself pulls in.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).parent.parent

CATALOGUE_MODULES = (
    "kubed.mcp_kb.catalogue.snapshot",
    "kubed.mcp_kb.catalogue.index",
    "kubed.mcp_kb.catalogue.uris",
    "kubed.mcp_kb.catalogue.skills",
    "kubed.mcp_kb.catalogue.harvest",
    "kubed.mcp_kb.catalogue.prompts",
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
).format(
    repo=str(REPO),
    mcp_kb=str(REPO / "kubed" / "mcp_kb"),
    modules=CATALOGUE_MODULES,
)


def test_catalogue_imports_no_fastmcp():
    """Every catalogue module imports clean with fastmcp unreachable.

    Proved non-vacuous against the pre-split tree, where ``catalogue/index.py``
    and ``catalogue/snapshot.py`` imported ``FilePrompt`` from
    ``mcp/prompts.py`` -- which imports ``fastmcp`` itself -- and this test
    failed with exactly the blocked-import error.
    """
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
