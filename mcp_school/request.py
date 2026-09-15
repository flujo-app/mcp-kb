"""What the current request says about itself.

Two questions, both answered from the HTTP request and both meaningless off it:
*which pack is this client pinned to*, and *can it read resources at all*. They
are read here rather than in the handlers because both halves of the server --
the resources and the tools that mirror them -- have to agree on the answers,
and a second reader is how the two halves drift apart.

Every helper degrades to the permissive default outside an HTTP request, which
is what stdio is: no headers, no query string, nothing to narrow by.
"""

from __future__ import annotations

# Per-request scope. A client that sets this sees only that pack, whatever it
# asks for -- which is how one deployment serves several single-pack agents.
PACK_HEADER = "x-skill-pack"

# How a client declares it cannot read resources. Header beats parameter: the
# header is set in a credential, by an admin, where the parameter rides on a URL
# somebody may paste without it.
RESOURCES_PARAM = "resources"
RESOURCES_HEADER = "x-mcp-resources"
_OFF = ("off", "false", "0", "no", "none")

# How a skill-syncing client asks for every skill in the listing. Off by default
# because it is the expensive shape and almost nothing needs it.
LISTING_PARAM = "skills"
LISTING_HEADER = "x-skill-listing"
_FULL = "full"


def http_request() -> tuple[dict, dict] | None:
    """(query params, headers) for the current request, or None off HTTP.

    Both are looked up through FastMCP's dependency helpers, which read the
    request from a context variable set by the ASGI stack.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers, get_http_request
    except ImportError:  # pragma: no cover - fastmcp is a hard dependency
        return None
    try:
        request = get_http_request()
    except Exception:  # noqa: BLE001 - outside an HTTP request this raises
        request = None
    if request is None:
        # Headers may still be reachable when the request object is not.
        try:
            headers = get_http_headers()
        except Exception:  # noqa: BLE001
            return None
        if not headers:
            return None
        return {}, {k.lower(): v for k, v in headers.items()}
    return (
        dict(request.query_params),
        {k.lower(): v for k, v in request.headers.items()},
    )


def requested_pack() -> str:
    """The pack this request is pinned to, or "" when it is unpinned.

    Read from the ``X-Skill-Pack`` header, which a client sets once in its
    connection config. It is a ceiling, not a suggestion: the model can narrow
    further with a ``pack`` argument but can never widen past it. That is the
    difference between a scope an agent has and one it merely was asked to keep.
    """
    http = http_request()
    if http is None:
        return ""
    _, headers = http
    return headers.get(PACK_HEADER, "").strip()


def client_reads_resources() -> bool:
    """Whether this caller can be expected to read MCP resources.

    Defaults to true -- the assumption is that a client is spec-complete, and a
    client that is not says so, with ``?resources=off`` on the MCP URL or an
    ``X-MCP-Resources: off`` header.
    """
    http = http_request()
    if http is None:
        return True
    params, headers = http
    declared = headers.get(RESOURCES_HEADER) or params.get(RESOURCES_PARAM)
    if declared is None:
        return True
    return str(declared).strip().lower() not in _OFF


def full_listing() -> bool:
    """Whether to list every skill rather than just the indexes.

    A tool that syncs skills to disk (FastMCP's ``sync_skills`` and friends)
    finds them only by scanning ``resources/list`` for ``/SKILL.md``, so for
    those clients the cheap listing is an empty one. Everything else pays ~16k
    tokens per listing for rows it was going to narrow down anyway, which is why
    this is opt-in: ``?skills=full``, or an ``X-Skill-Listing: full`` header.
    """
    http = http_request()
    if http is None:
        return False
    params, headers = http
    declared = headers.get(LISTING_HEADER) or params.get(LISTING_PARAM) or ""
    return str(declared).strip().lower() == _FULL
