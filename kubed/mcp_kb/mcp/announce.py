"""Telling a session its catalogue moved, once, with no broadcast to send it on."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mcp_types
from fastmcp.server.middleware import Middleware

from .request import http_request

if TYPE_CHECKING:
    from ..server import KnowledgeBase

# Session state key for the generation a client has already been told about.
GENERATION_KEY = "mcp-kb.generation"

# The header that says an HTTP connection has a session at all.
SESSION_HEADER = "mcp-session-id"


class AnnounceChanges(Middleware):
    """Tell each session, once, that the catalogue it listed has been rebuilt.

    MCP has no "the server changed" broadcast a stateless HTTP deployment can
    rely on, so this rides the next request the client makes anyway: compare the
    generation it was last told about against the live one, and if they differ,
    send the two list-changed notifications before answering.

    Once per move, per session, which is what the stored state buys. Sending on
    every request would have every client re-listing constantly; sending only on
    a listing would leave a client that never lists again holding stale URIs.

    A session's first request stores the current generation without announcing
    anything -- it has nothing stale to discard.

    Which leaves the connections that cannot remember. MCP 2026-07-28 dropped
    sessions, and FastMCP 4.0.3 serves such a connection by minting a fresh
    session id per request: ``set_state`` there writes an entry nobody will
    ever read, one per request, into a store with a day-long TTL. So an HTTP
    request arriving without an ``mcp-session-id`` is left alone entirely --
    correct as well as cheap, since a client with no session across requests
    has no listing to invalidate. stdio and in-memory connections have no such
    header to check at all, so ``_can_remember`` defaults to True for them and
    they fall through to the state calls too -- which, under FastMCP 4.0.3,
    turn out to be just as wasted, since those transports mint a fresh state
    key per request as well. Wasted, not wrong: the calls are still guarded,
    so this must never turn a working request into a failed one.
    """

    def __init__(self, knowledge_base: KnowledgeBase):
        self._knowledge_base = knowledge_base

    async def on_request(self, context, call_next):
        ctx = context.fastmcp_context
        if ctx is not None and _can_remember():
            await self._announce(ctx)
        return await call_next(context)

    async def _announce(self, ctx) -> None:
        generation = self._knowledge_base.generation
        try:
            told = await ctx.get_state(GENERATION_KEY)
        except Exception:  # noqa: BLE001 - no state here must not fail the request
            return
        if told == generation:
            return
        if told is not None:
            await ctx.send_notification(mcp_types.ResourceListChangedNotification())
            await ctx.send_notification(mcp_types.PromptListChangedNotification())
        try:
            await ctx.set_state(GENERATION_KEY, generation)
        except Exception:  # noqa: BLE001 - as above; remembering is best-effort
            return


def _can_remember() -> bool:
    """Whether this connection is worth trying to remember state against.

    Announcing once means remembering what was announced, and the only place to
    remember it is state keyed by the session. An HTTP request carrying no
    ``mcp-session-id`` header has no session to key on, so it is never worth
    trying: False.

    Anything that is not an HTTP request -- stdio, in-memory -- has no such
    header to check, so this defaults to True for it. That default is
    optimistic, not a guarantee: under FastMCP 4.0.3 neither keeps one
    continuous session either, minting a fresh state key per request just as a
    sessionless HTTP client does. The state calls made for them are therefore
    wasted, not merely redundant, but harmless: ``AnnounceChanges`` guards every
    one of them against a state store that will not read them back.
    """
    http = http_request()
    if http is None:
        return True
    _, headers = http
    return bool(headers.get(SESSION_HEADER))
