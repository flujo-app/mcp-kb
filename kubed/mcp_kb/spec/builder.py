"""The OpenAPI description of the HTTP surface.

The surface is two plain routes outside the MCP protocol -- `GET /health`,
`POST /reindex` -- so there is no tool schema to derive a request body from,
the way the sibling `selenium-flow` derives its browser actions'. What is
described here is entirely the response shape, kept level with `routes.py`'s
`report()` by hand; `tests/test_openapi.py` is what catches the two drifting.

`title`/`description` come from the installed distribution's metadata rather
than a parse of `pyproject.toml`: this runs inside the deployed image too, at
server registration, where there is no `pyproject.toml` to open, and
`importlib.metadata` is what `setuptools_scm` already derives the real
version from.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, metadata

DIST_NAME = "kubed-mcp-kb"

# The server's own name everywhere else -- the FastMCP instance, the console
# script, the image -- so the document calls itself that, not the distribution
# name a client never sees.
TITLE = "mcp-kb"

FALLBACK_DESCRIPTION = (
    "An MCP knowledge base: skills, prompts and agent material collected from "
    "git, WebDAV and folders into one catalogue, served as resources, or as "
    "tools for clients without them."
)

# What the committed openapi.yaml carries instead of a real version: required,
# but a real one would churn the file on every commit.
PLACEHOLDER_VERSION = "0.0.0"


def _info() -> tuple[str, str]:
    """(description, version), from the installed package when there is one."""
    try:
        found = metadata(DIST_NAME)
    except PackageNotFoundError:
        return FALLBACK_DESCRIPTION, PLACEHOLDER_VERSION
    return found["Summary"] or FALLBACK_DESCRIPTION, found["Version"]


def _field(kind: str, desc: str, **extra) -> dict:
    return {"type": kind, "description": desc, **extra}


# One entry per source-status outcome. `failed` is the only one with nothing
# else to say: there was never a tree to report counts or provenance from.
SOURCE_STATUS = {
    "type": "object",
    "description": (
        "One configured source, keyed by its name under `sources`. `library`, "
        "`skills`, `prompts`, `files`, `built` and `fingerprint` describe the "
        "harvest being served and are absent only when `status` is `failed`. "
        "`live`, `revalidated`, `fetched` and `cooling` appear only for a "
        "`cache: live` source -- one that revalidates files against a WebDAV "
        "server between harvests."
    ),
    "required": ["status"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "stale", "failed"]},
        "library": _field("string", "The library this source's skills join."),
        "skills": _field("integer", "Skills this source contributed."),
        "prompts": _field("integer", "Prompts this source contributed."),
        "files": _field("integer", "Library-level files this source contributed."),
        "built": _field(
            "string",
            "When the harvest served was built. Unchanged while `stale`: a "
            "failed refresh does not touch what is on disk.",
        ),
        "fingerprint": _field(
            "object",
            "A cheap summary of the tree this harvest was built from, which a "
            "refresh re-takes and compares to decide whether to rebuild. Its "
            "keys are the backend's own and are not a contract: a `file://` "
            "source counts `files` and `bytes` and takes the `newest` mtime, "
            "git reports the exported `commit` with the `ref` it was resolved "
            "from and the `remote` tip that ref names now, WebDAV the "
            "`exported` and `remote` ETag digests with a file count for each.",
            additionalProperties=True,
        ),
        "live": _field(
            "boolean",
            "Present and true only for a `cache: live` source, whether or "
            "not a read has happened yet.",
        ),
        "error": _field(
            "string",
            "Present when `stale` (why the last refresh failed, while the "
            "previous good harvest keeps serving) or `failed` (why nothing "
            "was ever harvested).",
        ),
        "skipped": {
            "type": "array",
            "description": (
                "Present when this source ships something the address space has "
                "no room for. Each entry is left out while the rest of the "
                "source serves: a library file whose address lies inside one of "
                "the source's own skills or is named `_index.md` or `_files.md`; "
                "a skill whose frontmatter `name` breaks the Agent Skills naming "
                "rule or differs from its directory, or that a skill before it "
                "already serves at that address; a prompt whose name a prompt "
                "before it already has. A clash with another source is not "
                "listed here: it fails the later source instead."
            ),
            "items": {
                "type": "object",
                "required": ["path", "reason"],
                "properties": {
                    "path": _field(
                        "string", "The file or skill directory, relative to the source."
                    ),
                    "reason": _field("string", "Why it is not served."),
                },
            },
        },
        "revalidated": _field(
            "integer",
            "Live sources only: files priced against the server since this "
            "snapshot was built.",
        ),
        "fetched": _field(
            "integer",
            "Live sources only: of those, the ones that had moved and were "
            "downloaded again.",
        ),
        "cooling": _field(
            "boolean",
            "Live sources only: present and true while a failed "
            "revalidation has this source's reads suspended.",
        ),
    },
}

HEALTH = {
    "type": "object",
    "description": (
        "Always 200: a source that failed to load is reported inside "
        "`sources`, never raised as a failed request."
    ),
    "required": [
        "status", "generation", "built", "libraries", "skills", "prompts", "sources",
    ],
    "properties": {
        "status": {"type": "string", "const": "ok"},
        "generation": _field(
            "integer", "Rebuilds served since the process started, from 0."
        ),
        "built": _field("string", "When the current snapshot was assembled."),
        "libraries": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Every library with a source in `ok` or `stale` status -- a "
                "`failed` source names no library."
            ),
        },
        "skills": _field("integer", "Skills in the current index."),
        "prompts": _field("integer", "Prompts currently served."),
        "sources": {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/SourceStatus"},
            "description": "One entry per configured source, keyed by its name.",
        },
    },
}

REINDEX = {
    "allOf": [
        {"$ref": "#/components/schemas/Health"},
        {
            "type": "object",
            "required": ["rebuilt"],
            "properties": {
                "rebuilt": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Source names rebuilt this pass -- every source, "
                        "since this endpoint always forces one, whether or "
                        "not its content moved."
                    ),
                }
            },
        },
    ]
}

REINDEX_ERROR = {
    "type": "object",
    "required": ["status", "error"],
    "properties": {
        "status": {"type": "string", "const": "error"},
        "error": _field(
            "string",
            "A fixed message: a refresh raised outside build_source's own "
            "per-source failure handling -- the one case this manual "
            "recovery lever can still fail at. The exception itself, with "
            "its traceback, goes to the server log, never this body.",
        ),
    },
}


def build_spec() -> dict:
    """Assemble the OpenAPI 3.1 document for `/health` and `/reindex`.

    3.1 for the same reason the sibling picks it: a strict superset of JSON
    Schema, so a future request schema drawn from a pydantic model never needs
    down-converting to fit.
    """
    description, version = _info()
    return {
        "openapi": "3.1.0",
        "info": {
            "title": TITLE,
            "version": version,
            "description": description,
            "license": {"name": "MIT", "identifier": "MIT"},
        },
        "servers": [
            {
                "url": "http://mcp-kb.flow.svc.cluster.local:8000",
                "description": "In-cluster Service.",
            },
            {"url": "http://localhost:8000", "description": "docker compose."},
        ],
        "tags": [{"name": "ops", "description": "Operational endpoints."}],
        # Explicit, not merely absent: this server has no auth at all, and a
        # linter that flags an operation with no `security` key cannot tell
        # "nobody declared one" from "one was declared and it is none".
        "security": [],
        "paths": {
            "/health": {
                "get": {
                    "operationId": "health",
                    "summary": (
                        "Readiness, and the fastest way to see which sources loaded."
                    ),
                    "description": (
                        "Ignores X-Skill-Library: an operator asking what "
                        "this pod serves wants the real catalogue, not one "
                        "client's scoped view of it."
                    ),
                    "tags": ["ops"],
                    "responses": {
                        "200": {
                            "description": (
                                "The catalogue this pod is serving right now."
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Health"}
                                }
                            },
                        }
                    },
                }
            },
            "/reindex": {
                "post": {
                    "operationId": "reindex",
                    "summary": "Rebuild every source now.",
                    "description": (
                        "Re-reads exactly the sources the config already "
                        "names -- what the background refresh loop would do "
                        "on its own -- and returns /health plus `rebuilt`. "
                        "Takes no input and needs no authorisation beyond "
                        "what the config has already decided."
                    ),
                    "tags": ["ops"],
                    "responses": {
                        "200": {
                            "description": "The catalogue after the rebuild.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Reindex"}
                                }
                            },
                        },
                        "500": {
                            "description": (
                                "The rebuild raised something no source's own "
                                "failure handling turned into a stale or "
                                "failed record."
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ReindexError"
                                    }
                                }
                            },
                        },
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "Health": HEALTH,
                "Reindex": REINDEX,
                "ReindexError": REINDEX_ERROR,
                "SourceStatus": SOURCE_STATUS,
            }
        },
    }
