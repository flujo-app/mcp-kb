"""Plain HTTP endpoints served alongside the MCP transport.

Operational, not agent-facing: these answer "what is this pod running?" for a
kubelet probe or a human with curl, and are deliberately outside the MCP
protocol so checking them needs no MCP client.

Both routes read the school's current snapshot when they are called, not one
captured at registration, so what they report is what the server is serving
right now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from .server import School


def register(mcp: FastMCP, school: School) -> None:
    """Register the HTTP routes on ``mcp``."""

    def report() -> dict:
        snapshot = school.snapshot
        libraries = sorted(
            {s["library"] for s in snapshot.status.values() if s.get("status") == "ok"}
        )
        return {
            "status": "ok",
            "generation": snapshot.generation,
            "built": snapshot.built,
            "libraries": libraries,
            "skills": len(snapshot.index),
            "prompts": len(snapshot.prompts),
            "sources": snapshot.status,
        }

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        """Readiness probe, and the fastest way to tell which sources loaded.

        Reports the catalogue as *loaded*, ignoring any ``X-Skill-Pack`` header,
        because an operator asking what this pod serves wants the real answer.
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
        The response is ``/health`` plus ``rebuilt``, the source names whose
        harvest actually changed.
        """
        rebuilt = await school.refresh_async(force=True)
        return JSONResponse({**report(), "rebuilt": rebuilt})
