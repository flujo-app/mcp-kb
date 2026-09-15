"""Plain HTTP endpoints served alongside the MCP transport.

Operational, not agent-facing: these answer "what is this pod running?" for a
kubelet probe or a human with curl, and are deliberately outside the MCP
protocol so checking them needs no MCP client.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .prompts import FilePrompt
from .skills import SkillIndex


def register(
    mcp: FastMCP, index: SkillIndex, prompts: Sequence[FilePrompt] = ()
) -> None:
    """Register the HTTP routes on ``mcp``."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        """Readiness probe, and the fastest way to tell which image is running.

        Reports the catalogue as *loaded*, ignoring any ``X-Skill-Pack`` header,
        because an operator asking what this pod serves wants the real answer.
        """
        return JSONResponse(
            {
                "status": "ok",
                "packs": index.packs,
                "skills": len(index),
                "prompts": len(prompts),
            }
        )
