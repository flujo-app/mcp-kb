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
# one deployment serves several narrowly-scoped agents.
LIBRARY_PARAM = "library"
LIBRARY_HEADER = "x-skill-library"
CATEGORIES_PARAM = "categories"
CATEGORIES_HEADER = "x-skill-categories"
TAGS_PARAM = "tags"
TAGS_HEADER = "x-skill-tags"

# How a client declares it cannot use MCP prompts, and so wants them as tools.
PROMPTS_PARAM = "prompts"
PROMPTS_HEADER = "x-mcp-prompts"

# How a client declares it cannot read resources.
RESOURCES_PARAM = "resources"
RESOURCES_HEADER = "x-mcp-resources"
_OFF = ("off", "false", "0", "no", "none")

# How a skill-syncing client asks for every skill in the listing. Off by default
# because it is the expensive shape and almost nothing needs it.
LISTING_PARAM = "skills"
LISTING_HEADER = "x-skill-listing"
_FULL = "full"


def http_request() -> tuple[dict[str, list[str]], dict[str, list[str]]] | None:
    """(query params, headers) for the current request, or None off HTTP.

    Both are looked up through FastMCP's dependency helpers, which read the
    request from a context variable set by the ASGI stack. Every value is a
    *list*, because repeating a parameter or a header is how a client says
    "any of these" -- and a reader that took `dict(query_params)` would keep
    the last one and silently narrow the scope it was handed.
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
        # A plain mapping: one value per name, which is a one-item list here.
        return {}, {k.lower(): [v] for k, v in headers.items()}
    return (
        {k: request.query_params.getlist(k) for k in request.query_params},
        {k.lower(): request.headers.getlist(k) for k in request.headers},
    )


def _read(header: str, param: str, *aliases: str) -> list[str]:
    """What this request says for one setting: every value, or none at all.

    Headers first, in the order given, then the query parameter: the header is
    set in a credential, by an admin, where the parameter rides on a URL
    somebody may paste without it. Off HTTP there is neither, and an empty list
    is what every caller reads as "not narrowed, not declared".
    """
    http = http_request()
    if http is None:
        return []
    params, headers = http
    for name in (header, *aliases):
        found = [value for value in headers.get(name, []) if value]
        if found:
            return found
    return [value for value in params.get(param, []) if value]


def requested_scope() -> Scope:
    """The slice of the catalogue this request is restricted to.

    Read from the MCP URL (``?library=grafana&categories=observability``) or
    headers (``X-Skill-Library``, ``X-Skill-Categories``, ``X-Skill-Tags``). It
    is a ceiling set by whoever configured the client, not a suggestion the
    model can widen. One library: it is the URI's first segment, so several
    would be a different request.
    """
    library = _read(LIBRARY_HEADER, LIBRARY_PARAM)
    return Scope.parse(
        library[0] if library else "",
        _read(CATEGORIES_HEADER, CATEGORIES_PARAM),
        _read(TAGS_HEADER, TAGS_PARAM),
    )


def _declared_on(header: str, param: str) -> bool:
    """True unless the client said ``off`` for this capability."""
    declared = _read(header, param)
    return not declared or declared[0].strip().lower() not in _OFF


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
    declared = _read(LISTING_HEADER, LISTING_PARAM)
    return bool(declared) and declared[0].strip().lower() == _FULL
