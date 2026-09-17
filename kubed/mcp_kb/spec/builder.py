"""The OpenAPI description of the HTTP surface.

The surface is two plain routes outside the MCP protocol -- `GET /health`,
`POST /reindex` -- so there is no tool schema to derive a request body from,
the way the sibling `selenium-flow` derives its browser actions'. What is
described here is entirely the response shape, kept level with `routes.py`'s
`report()` and `snapshot.py`'s status entries by hand; `tests/test_openapi.py`
is what catches them drifting.

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


SKIPPED = {
    "type": "array",
    "description": (
        "Present when this plugin ships something the address space has no "
        "room for. Each entry is left out while the rest of the plugin serves: "
        "a library file whose address lies inside one of the plugin's own "
        "skills or is named `_index.md` or `_files.md`; a skill whose "
        "frontmatter `name` breaks the Agent Skills naming rule or differs "
        "from its directory, or that a skill before it already serves at that "
        "address; a prompt whose name a prompt before it already has, or whose "
        "file does not parse or cannot be read. A clash with another plugin is "
        "not listed here: it fails the later plugin in that library instead, "
        "under the library's `conflicts`."
    ),
    "items": {
        "type": "object",
        "required": ["path", "reason"],
        "properties": {
            "path": _field(
                "string", "The file or skill directory, relative to the plugin root."
            ),
            "reason": _field("string", "Why it is not served."),
        },
    },
}

# One entry per fetch: a tree materialised once, however many plugins read it.
FETCH_STATUS = {
    "type": "object",
    "description": (
        "One materialised tree, keyed under `fetches` by its fetch key -- the "
        "plugin address without its subdirectory. `built` and `fingerprint` "
        "describe the tree being served and are absent only when `status` is "
        "`failed`. `live`, `revalidated`, `fetched` and `cooling` appear only "
        "for a `cache: live` source -- one that revalidates files against a "
        "WebDAV server between harvests."
    ),
    "required": ["status"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "stale", "failed"]},
        "built": _field(
            "string",
            "When the tree served was materialised. Unchanged while `stale`: a "
            "failed refresh does not touch what is on disk.",
        ),
        "fingerprint": _field(
            "object",
            "A cheap summary of the tree, which a refresh re-takes and compares "
            "to decide whether to rebuild. Its keys are the backend's own and "
            "are not a contract: a `file://` fetch counts `files` and `bytes` "
            "and takes the `newest` mtime, git reports the exported `commit` "
            "with the `ref` it was resolved from and the `remote` tip that ref "
            "names now, WebDAV the `exported` and `remote` ETag digests with a "
            "file count for each.",
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
            "previous good tree keeps serving) or `failed` (why nothing was "
            "ever materialised).",
        ),
        "revalidated": _field(
            "integer",
            "Live fetches only: files priced against the server since this "
            "snapshot was built.",
        ),
        "fetched": _field(
            "integer",
            "Live fetches only: of those, the ones that had moved and were "
            "downloaded again.",
        ),
        "cooling": _field(
            "boolean",
            "Live fetches only: present and true while a failed "
            "revalidation has this fetch's reads suspended.",
        ),
    },
}

# One entry per plugin, declared or read out of a marketplace.
PLUGIN_STATUS = {
    "type": "object",
    "description": (
        "One plugin, keyed under `plugins` by its id: its config name, or "
        "`<entry>@<library>` for a marketplace entry. `skills`, `prompts`, "
        "`files` and `built` are the plugin's own harvest, independent of "
        "which libraries serve it, and are absent when `status` is `failed`."
    ),
    "required": [
        "status", "fetch", "root", "category", "tags", "keywords", "libraries",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "failed"]},
        "fetch": _field("string", "The key of the fetch this plugin is read from."),
        "root": _field(
            "string",
            "The plugin's directory inside its fetch; empty at the tree's root.",
        ),
        "category": {
            "type": ["string", "null"],
            "description": "The plugin's category, or null when it declares none.",
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "version": _field("string", "Present when the plugin declares one."),
        "libraries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The libraries that serve this plugin, in config order.",
        },
        "skills": _field("integer", "Skills this plugin yields."),
        "prompts": _field("integer", "Prompts this plugin yields."),
        "files": _field("integer", "Library-level files this plugin yields."),
        "built": _field("string", "When this plugin was last harvested."),
        "error": _field(
            "string",
            "Present when `failed`: its fetch failed, its directory is not in "
            "the tree, or its manifest does not parse.",
        ),
        "skipped": SKIPPED,
    },
}

# One entry per configured library.
LIBRARY_STATUS = {
    "type": "object",
    "description": (
        "One configured library, keyed under `libraries` by its name. The "
        "counts are what it serves right now, summed over its plugins."
    ),
    "required": ["description", "plugins", "skills", "prompts", "files"],
    "properties": {
        "description": _field("string", "The library's description, from the config."),
        "plugins": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The plugin ids this library resolved to: its marketplace's "
                "entries, then its `plugins:`, then its selector's matches."
            ),
        },
        "skills": _field("integer", "Skills served in this library."),
        "prompts": _field("integer", "Prompts served in this library."),
        "files": _field("integer", "Library-level files served in this library."),
        "error": _field(
            "string",
            "Present when part of the library could not be resolved -- its "
            "marketplace could not be read, or a named plugin does not exist. "
            "The rest of the library serves.",
        ),
        "skipped": {
            "type": "array",
            "description": (
                "Present when the library's marketplace lists entries this "
                "server does not install: `npm`, `archive` and `command` "
                "sources, `strict: false` entries, and entries that are "
                "malformed."
            ),
            "items": {
                "type": "object",
                "required": ["plugin", "reason"],
                "properties": {
                    "plugin": _field("string", "The entry's name, or `?`."),
                    "reason": _field("string", "Why it is not installed."),
                },
            },
        },
        "conflicts": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "Present when a plugin of this library would serve an address "
                "or a prompt name an earlier plugin of it already serves. Keyed "
                "by plugin id; that plugin is not served in this library, and "
                "serves normally in any other."
            ),
        },
    },
}

HEALTH = {
    "type": "object",
    "description": (
        "Always 200: a fetch or a plugin that failed to load is reported "
        "inside `fetches` and `plugins`, never raised as a failed request."
    ),
    "required": [
        "status", "generation", "built", "skills", "prompts",
        "libraries", "plugins", "fetches",
    ],
    "properties": {
        "status": {"type": "string", "const": "ok"},
        "generation": _field(
            "integer", "Rebuilds served since the process started, from 0."
        ),
        "built": _field("string", "When the current snapshot was assembled."),
        "skills": _field("integer", "Skills in the current index."),
        "prompts": _field("integer", "Prompts currently served."),
        "libraries": {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/LibraryStatus"},
            "description": "One entry per configured library, keyed by name.",
        },
        "plugins": {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/PluginStatus"},
            "description": (
                "One entry per plugin, declared or read out of a marketplace, "
                "keyed by id."
            ),
        },
        "fetches": {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/FetchStatus"},
            "description": "One entry per materialised tree, keyed by fetch key.",
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
                        "Fetch keys rebuilt this pass -- every fetch, since "
                        "this endpoint always forces one, whether or not its "
                        "content moved."
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
            "A fixed message: a refresh raised outside build_fetch's own "
            "per-fetch failure handling -- the one case this manual "
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
                        "Readiness, and the fastest way to see which fetches loaded."
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
                    "summary": "Rebuild every fetch now.",
                    "description": (
                        "Re-reads exactly the fetches the config's plugins and "
                        "marketplaces name -- what the background refresh loop "
                        "would do on its own -- and returns /health plus `rebuilt`. "
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
                                "The rebuild raised something no fetch's own "
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
                "LibraryStatus": LIBRARY_STATUS,
                "PluginStatus": PLUGIN_STATUS,
                "FetchStatus": FETCH_STATUS,
            }
        },
    }
