"""Plain HTTP endpoints served alongside the MCP transport.

Operational, not agent-facing: these answer "what is this pod running?" for a
kubelet probe or a human with curl, and are deliberately outside the MCP
protocol so checking them needs no MCP client.

``/health`` and ``/reindex`` read the knowledge base's current snapshot when
they are called, not one captured at registration, so what they report is
what the server is serving right now. ``/openapi.yaml`` describes those two
routes and is the one built once, at registration -- see ``spec/builder.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .spec import build_spec

if TYPE_CHECKING:
    from .server import KnowledgeBase

log = logging.getLogger(__name__)

# `/reindex` is unauthenticated: `str(exc)` can carry an internal path or a
# remote URL, so the caller gets this fixed message and the real exception,
# with its traceback, goes to the log instead.
REINDEX_FAILURE_MESSAGE = "reindex failed; see the server log"


def register(mcp: FastMCP, knowledge_base: KnowledgeBase) -> None:
    """Register the HTTP routes on ``mcp``."""
    # Built once, here, rather than per request or lazily on first hit: the
    # document describes the routes this function registers, not the
    # catalogue, so nothing about it can change while the process runs.
    spec_yaml = yaml.safe_dump(build_spec(), sort_keys=False, width=100)

    def report() -> dict:
        snapshot = knowledge_base.snapshot
        # A stale source is still being served from its last good harvest, so
        # its library is still here; only a failed one has nothing to list.
        libraries = sorted(
            {
                s["library"]
                for s in snapshot.status.values()
                if s.get("status") in ("ok", "stale")
            }
        )
        return {
            "status": "ok",
            "generation": snapshot.generation,
            "built": snapshot.built,
            "libraries": libraries,
            "skills": len(snapshot.index),
            "prompts": len(snapshot.prompts),
            # The counters are read at report time, not at build time: a
            # live source's reads happen long after its snapshot was made.
            "sources": {
                name: {**info, **snapshot.stats(name)}
                for name, info in snapshot.status.items()
            },
        }

    @mcp.custom_route("/openapi.yaml", methods=["GET"])
    async def openapi_yaml(_request: Request) -> Response:
        """This HTTP surface as OpenAPI 3.1.

        Unauthenticated, like /health: there is no auth on this server at
        all, so there is no credential to gate a description of it behind.
        """
        return Response(spec_yaml, media_type="text/yaml")

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        """Readiness probe, and the fastest way to tell which sources loaded.

        Reports the catalogue as *loaded*, ignoring any ``X-Skill-Library``
        header, because an operator asking what this pod serves wants the real
        answer.
        ``status`` is ``"ok"`` even when a source failed to materialise --
        readiness stays green for whatever did load, and the failure shows up in
        ``sources`` instead, keyed by source name. Each source also carries the
        ``built`` timestamp and ``fingerprint`` of the harvest being served, so
        "is this pod stale?" is answerable without an MCP client.
        """
        return JSONResponse(report())

    @mcp.custom_route("/reindex", methods=["POST"])
    async def reindex(_request: Request) -> JSONResponse:
        """Rebuild every source now, and report the catalogue that results.

        Unauthenticated on purpose. This server is read-only, and the endpoint
        takes no input: it re-reads exactly the sources the config already
        names, which the background loop would re-read on its own anyway. There
        is nothing here to authorise that the config has not already decided.
        The response is ``/health`` plus ``rebuilt``, the source names actually
        rebuilt this pass -- with ``force=True`` that is every source that did
        not fail identically to how it already had, changed or not, not only
        the ones whose content moved.

        A rebuild that raises something ``build_source`` did not already turn
        into a failed record must still answer: this is the only manual
        recovery lever the server has, and an unhandled 500 would take it away
        for good.
        """
        try:
            rebuilt = await knowledge_base.refresh_async(force=True)
        except Exception:
            log.exception("reindex failed")
            return JSONResponse(
                {"status": "error", "error": REINDEX_FAILURE_MESSAGE}, status_code=500
            )
        return JSONResponse({**report(), "rebuilt": rebuilt})
