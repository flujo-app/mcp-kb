"""Entry point: turn CLI flags and environment into a running server.

Every flag has an environment fallback because the container is configured with
env vars while a developer reaches for flags. The config file, `CONFIG`, says
*what* to serve; these flags say *how* to run it (transport, port, cache
directory), and the split never blurs. The one other reader of the environment
is `EnvRef.resolve` in `config.py`, which resolves a source's credential where
it is declared rather than passing it through a plain `str` on the way.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config, schema
from .server import KnowledgeBase

DEFAULT_CONFIG = Path("/etc/mcp-kb/config.yaml")
DEFAULT_CACHE_DIR = Path("/var/cache/mcp-kb")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-kb",
        description=(
            "Serve a knowledge base of skills, prompts and agent material "
            "over MCP."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "schema"],
        help="serve the catalogue, or print its config JSON Schema (default: serve)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("CONFIG", DEFAULT_CONFIG)),
        help="config file listing the sources to serve (env: CONFIG)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("CACHE_DIR", DEFAULT_CACHE_DIR)),
        help="directory a non-file:// source materialises into (env: CACHE_DIR)",
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
    """Entry point for the ``mcp-kb`` console script."""
    args = build_parser().parse_args(argv)
    if args.command == "schema":
        print(json.dumps(schema(), indent=2))
        return
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # A bad config must fail loudly at boot, not silently serve nothing.
        print(f"mcp-kb: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    server = KnowledgeBase(config, args.cache_dir)
    server.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
