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
import logging
import os
import sys
import time
from pathlib import Path

from .config import ConfigError, load_config, schema
from .server import KnowledgeBase
from .sources import AccessRefused

DEFAULT_CONFIG = Path("/etc/mcp-kb/config.yaml")
DEFAULT_CACHE_DIR = Path("/var/cache/mcp-kb")
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# The loggers that arrive with a handler of their own. Each is emptied and made
# to propagate, so one line format reaches the collector whoever wrote it.
THIRD_PARTY_LOGGERS = ("fastmcp", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access")


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
    parser.add_argument(
        "--log-level",
        type=str.upper,
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=LOG_LEVELS,
        help="the least severe log line written, for every logger (env: LOG_LEVEL)",
    )
    return parser


def configure_logging(level: str) -> None:
    """One handler on stderr, one line format, one level, for the whole process.

    FastMCP installs a Rich handler of its own on import and uvicorn installs
    its own on start, each with its own format and level, so ``LOG_LEVEL`` set
    only on this package would leave most of what the process writes untouched.
    Each is emptied and sent to the root handler instead.

    uvicorn's access log is the exception: a line per MCP call and per probe
    is traffic, not what the server did, so it is written only at ``DEBUG``.
    """
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in THIRD_PARTY_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
    if level != "DEBUG":
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``mcp-kb`` console script."""
    args = build_parser().parse_args(argv)
    if args.command == "schema":
        print(json.dumps(schema(), indent=2))
        return
    configure_logging(args.log_level)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # A bad config must fail loudly at boot, not silently serve nothing.
        print(f"mcp-kb: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        server = KnowledgeBase(config, args.cache_dir)
    except AccessRefused as exc:
        # Exit rather than serve without it: see KnowledgeBase._cold_start.
        print(f"mcp-kb: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc
    server.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
