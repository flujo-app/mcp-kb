"""What the current request says about itself.

Two kinds of question, both answered from the HTTP request and both meaningless off it:
*what slice of the catalogue may this client see*, and *which MCP features
can it use natively*. They
are read here rather than in the handlers because both halves of the server --
the resources and the tools that mirror them -- have to agree on the answers,
and a second reader is how the two halves drift apart.

Every helper degrades to the permissive default outside an HTTP request, which
is what stdio is: no headers, no query string, nothing to narrow by.
"""

from __future__ import annotations

from .scope import Scope

# Per-request scope, set once in a client's connection config -- which is how
# one deployment serves several narrowly-scoped agents. X-Skill-Pack predates
# libraries and is kept as an alias for X-Skill-Library.
LIBRARY_PARAM = "library"
LIBRARY_HEADER = "x-skill-library"
PACK_HEADER = "x-skill-pack"
TAGS_PARAM = "tags"
TAGS_HEADER = "x-skill-tags"

# How a client declares it cannot use MCP prompts, and so wants them as tools.
PROMPTS_PARAM = "prompts"
PROMPTS_HEADER = "x-mcp-prompts"

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


def requested_scope() -> Scope:
    """The slice of the catalogue this request is restricted to.

    Read from the MCP URL (``?library=grafana&tags=observability``) or headers
    (``X-Skill-Library``, ``X-Skill-Tags``; ``X-Skill-Pack`` as an alias). A
    header beats a parameter for the reason given above. It is a ceiling set by
    whoever configured the client, not a suggestion the model can widen.
    """
    http = http_request()
    if http is None:
        return Scope()
    params, headers = http
    library = (
        headers.get(LIBRARY_HEADER)
        or headers.get(PACK_HEADER)
        or params.get(LIBRARY_PARAM)
        or ""
    )
    tags = headers.get(TAGS_HEADER) or params.get(TAGS_PARAM) or ""
    return Scope.parse(library, tags)


def _declared_on(header: str, param: str) -> bool:
    """True unless the client said ``off`` for this capability."""
    http = http_request()
    if http is None:
        return True
    params, headers = http
    declared = headers.get(header) or params.get(param)
    if declared is None:
        return True
    return str(declared).strip().lower() not in _OFF


def client_uses_prompts() -> bool:
    """Whether this caller can be expected to use MCP prompts natively.

    The protocol has no client-side signal for it -- prompts are a server
    capability -- so, like resources, a client that cannot says so:
    ``?prompts=off`` or ``X-MCP-Prompts: off``.
    """
    return _declared_on(PROMPTS_HEADER, PROMPTS_PARAM)


def client_reads_resources() -> bool:
    """Whether this caller can be expected to read MCP resources.

    Defaults to true -- the assumption is that a client is spec-complete, and a
    client that is not says so, with ``?resources=off`` on the MCP URL or an
    ``X-MCP-Resources: off`` header.
    """
    return _declared_on(RESOURCES_HEADER, RESOURCES_PARAM)


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
