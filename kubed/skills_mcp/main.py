"""Entry point: turn CLI flags and environment into a running server.

Every flag has an environment fallback because the container is configured with
env vars while a developer reaches for flags. Nothing else in the package reads
the environment, so this file is the whole configuration surface.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .server import DEFAULT_PROMPTS_DIR, DEFAULT_SKILLS_DIR, SkillsMCP


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skills-mcp", description="Serve Agent Skills over MCP."
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=Path(os.environ.get("SKILLS_DIR", DEFAULT_SKILLS_DIR)),
        help="directory to scan for skills (env: SKILLS_DIR)",
    )
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        default=Path(os.environ.get("PROMPTS_DIR", DEFAULT_PROMPTS_DIR)),
        help="directory holding <pack>/<name>.md prompts (env: PROMPTS_DIR)",
    )
    parser.add_argument(
        "--packs",
        default=os.environ.get("SKILL_PACKS", ""),
        help="comma-separated packs to serve; empty serves all (env: SKILL_PACKS)",
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("TRANSPORT", "http"),
        choices=["stdio", "http"],
        help="transport to serve on (env: TRANSPORT)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="bind address for http transport (env: HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="port for http transport (env: PORT)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``skills-mcp`` console script."""
    args = build_parser().parse_args(argv)
    packs = [p.strip() for p in args.packs.split(",") if p.strip()] or None
    server = SkillsMCP(args.skills_dir, packs, args.prompts_dir)
    server.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
