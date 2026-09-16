"""The MCP server itself: wiring, and the lifecycle of what it serves.

Assembles a FastMCP instance from the catalogue a config file describes -- the
address space, the resources, the mirror tools, the routes -- and runs it on a
transport. Turning a config source into a directory is ``sources``'s job;
deciding what in it counts as a skill, a prompt or a pack-level file is
``harvest.py``'s; building one immutable view of all of it is ``snapshot.py``'s;
the catalogue itself lives in ``skills.py``, ``prompts.py`` and ``uris.py``.
This module walks the config in source order, connects the pieces, and owns the
one mutable thing in the process: which ``Snapshot`` is current.

Three rules follow from that, and every change here has to keep them:

- **Start from the index, not from the sources.** ``<cache>/index.json`` holds
  what each source yielded last time. If it was built from this same config,
  boot reuses every record whose tree is still on disk and rebuilds only the
  rest. A pod restart is then milliseconds and touches no network, and the
  question "has anything changed?" is deferred to the background pass that runs
  a moment later.
- **Swap, never mutate.** A rebuild constructs a whole new ``Snapshot`` beside
  the live one and replaces it with a single assignment. The old snapshot keeps
  serving until that instant, so there is no window in which the server has a
  partial catalogue, and a request already in flight finishes against the
  consistent one it started with.
- **Read through the getter.** Every provider, tool and route is handed a
  ``lambda: self.snapshot...``, never a ``Catalogue``. A stored one would pin
  that component to the generation it was registered in, which is a bug that
  looks exactly like "the refresh does not work".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import mcp_types
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from . import prompts, resources, routes, tools
from .config import Config, Source
from .index import INDEX_VERSION, Index, SourceRecord, config_hash, now
from .prompts import FilePrompt
from .request import http_request
from .skills import PackResources, SkillIndex
from .snapshot import Snapshot, build_snapshot, build_source, record_from_index
from .sources import SourceError, fingerprint
from .uris import Catalogue

log = logging.getLogger(__name__)

INDEX_FILE = "index.json"

# How often the background loop wakes to ask what is due. A source's own
# ``refresh`` decides when it is actually rebuilt; this only bounds how late
# that can be, and a short tick costs nothing because a tick with nothing due
# does no work at all.
TICK_SECONDS = 5

# Session state key for the generation a client has already been told about.
GENERATION_KEY = "mcp-school.generation"

# The header that says an HTTP connection has a session at all.
SESSION_HEADER = "mcp-session-id"

INSTRUCTIONS = """\
This server hosts Agent Skills: instruction packages that teach you how to \
perform a specific task. Everything it serves is a `skill://` URI, and reading \
one is the only operation there is.

Work down the address space, cheapest first. Listing gives you indexes -- one \
per pack, one per group within a pack. Reading an index URI \
(`skill://grafana-lgtm`) gives you the skills in it, as URIs. Reading a skill \
URI (`skill://grafana/loki`) gives you the instructions to follow.

A skill may ship supporting files. Append `/_manifest` to its URI to list them, \
then read one by its path under the same URI. Do not read files you have no use \
for -- a skill citing one is not a reason to fetch it.
"""


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

    def __init__(self, school: School):
        self._school = school

    async def on_request(self, context, call_next):
        ctx = context.fastmcp_context
        if ctx is not None and _can_remember():
            await self._announce(ctx)
        return await call_next(context)

    async def _announce(self, ctx) -> None:
        generation = self._school.generation
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


class School:
    """An MCP server over the sources a config file names.

    The catalogue is served twice, because MCP clients are not all alike. As
    ``skill://`` **resources**, which is what it is; and as two **tools** that
    mirror those resources exactly, for the many clients that only implement
    tools -- n8n among them, to which a resource-only server looks empty.

    The mirror is hidden from clients that read resources, so each client sees
    one way to ask, not two. A client declares it cannot read resources with
    ``?resources=off`` on the MCP URL or an ``X-MCP-Resources: off`` header.
    """

    def __init__(self, config: Config, cache: Path):
        self.config = config
        self.cache = cache
        self.index_path = cache / INDEX_FILE
        # Every write of self._records and self.snapshot happens under this,
        # so two refreshes triggered at once still swap in a single order.
        self._lock = threading.Lock()
        # When each source was last examined, keyed by monotonic time rather
        # than a record's `built` -- `built` only moves when a rebuild actually
        # replaces a record, and scheduling off it is what let an unchanged (or
        # persistently failing) source come due on every tick forever. See
        # `_due`.
        self._checked: dict[str, float] = {}

        self._records = self._cold_start()
        self.snapshot: Snapshot = build_snapshot(config, self._records, generation=0)
        self._write_index()

        ttl = config.min_refresh_seconds
        self.mcp = FastMCP(
            "mcp-school",
            instructions=INSTRUCTIONS,
            lifespan=self._lifespan,
            # A client may hold a listing for as long as the shortest refresh
            # interval, which is the soonest it could possibly be wrong. That
            # is the whole staleness bound for a sessionless client, which
            # AnnounceChanges cannot reach. Private, because a listing is
            # scoped by the caller's own headers.
            cache_ttl=ttl,
            cache_scope=None if ttl is None else "private",
        )

        resources.register(self.mcp, lambda: self.snapshot.catalogue)
        mirrors = tools.register(self.mcp, lambda: self.snapshot.catalogue)
        self.mcp.add_middleware(resources.HideMirrorTools(mirrors))
        self.mcp.add_middleware(AnnounceChanges(self))
        prompts.register(self.mcp, lambda: self.snapshot)
        routes.register(self.mcp, self)

    # -- what the snapshot currently holds -----------------------------------

    @property
    def generation(self) -> int:
        return self.snapshot.generation

    @property
    def index(self) -> SkillIndex:
        return self.snapshot.index

    @property
    def resources(self) -> PackResources:
        return self.snapshot.resources

    @property
    def catalogue(self) -> Catalogue:
        return self.snapshot.catalogue

    @property
    def prompts(self) -> tuple[FilePrompt, ...]:
        return self.snapshot.prompts

    @property
    def status(self) -> dict[str, dict]:
        return self.snapshot.status

    # -- building it ---------------------------------------------------------

    def _cold_start(self) -> dict[str, SourceRecord]:
        """Every source's record, reusing the on-disk index wherever it holds.

        No fingerprint is taken here. Checking one means walking every source
        tree, which is the cost this whole file exists to move off the boot
        path; the verification pass the lifespan runs immediately afterwards
        does it instead, and until it finishes the server serves last boot's
        answer -- which is almost always still the right one.
        """
        stored = Index.read(self.index_path)
        reusable: dict[str, SourceRecord] = {}
        if stored is not None and stored.config_hash == config_hash(self.config):
            reusable = stored.sources
        elif stored is not None:
            log.info("config changed since %s was written; rebuilding", self.index_path)

        records: dict[str, SourceRecord] = {}
        for source in self.config.sources:
            kept = reusable.get(source.name)
            from_index = (
                record_from_index(self.config, source, kept)
                if kept is not None
                else None
            )
            records[source.name] = from_index or build_source(
                self.config, source, self.cache
            )
        return records

    def _write_index(self) -> None:
        """Persist the current records, or log why the next boot will be slow.

        A cache directory that cannot be written is not fatal: the catalogue is
        already built and serving. It only means the next cold start harvests
        everything again, which is exactly what happened before there was an
        index at all.
        """
        record = Index(
            version=INDEX_VERSION,
            built=now(),
            config_hash=config_hash(self.config),
            sources=self._records,
        )
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            record.write(self.index_path)
        except OSError as exc:
            log.warning("could not write %s: %s", self.index_path, exc)

    def refresh(
        self, *, force: bool = False, only: Iterable[str] | None = None
    ) -> list[str]:
        """Rebuild whatever has changed and swap in a new snapshot. Blocking.

        Returns the names actually rebuilt, in config order -- empty when
        nothing moved, which is the common case and the reason the generation
        does not advance on every pass. ``only`` narrows the sweep to the
        sources the caller already knows are due; ``force`` skips the
        fingerprint check and rebuilds them regardless.

        Synchronous and serialised: this walks trees, hashes files and writes
        ``index.json``, and two of them at once would race for the index's
        temporary file. Call it from the event loop through ``refresh_async``.
        """
        names = None if only is None else set(only)
        checked_at = time.monotonic()
        with self._lock:
            records = dict(self._records)
            rebuilt: list[str] = []
            for source in self.config.sources:
                if names is not None and source.name not in names:
                    continue
                current = records.get(source.name)
                # Examined, whether or not it turns out stale -- this is what
                # `_due` schedules off, so it must move on every check, not
                # only on a rebuild.
                self._checked[source.name] = checked_at
                if (
                    not force
                    and current is not None
                    and not self._stale(source, current)
                ):
                    continue
                fresh = build_source(self.config, source, self.cache)
                if _same_failure(current, fresh):
                    continue
                records[source.name] = fresh
                rebuilt.append(source.name)

            if not rebuilt:
                return []
            snapshot = build_snapshot(
                self.config, records, generation=self.snapshot.generation + 1
            )
            self._records = records
            self.snapshot = snapshot
            # Best-effort and therefore last: a raise here must not leave
            # _records advanced while snapshot still holds the old generation.
            self._write_index()
            return rebuilt

    async def refresh_async(self, **kwargs) -> list[str]:
        """``refresh`` off the event loop, because all of it blocks."""
        return await asyncio.to_thread(self.refresh, **kwargs)

    def _stale(self, source: Source, record: SourceRecord) -> bool:
        """Whether ``source`` has moved on since ``record`` was built."""
        if record.status != "ok" or record.root is None:
            return True
        root = Path(record.root)
        if not root.is_dir():
            return True
        try:
            return fingerprint(source, self.cache, root) != record.fingerprint
        except SourceError:
            return True

    # -- keeping it current --------------------------------------------------

    @contextlib.asynccontextmanager
    async def _lifespan(self, server: FastMCP) -> AsyncIterator[dict]:
        """Run the refresh loop for as long as the server is up."""
        del server
        task = asyncio.create_task(self._refresh_loop())
        try:
            yield {}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _refresh_loop(self) -> None:
        """One verification pass, then a pass per tick for whatever is due.

        The first pass is the other half of the cold start: boot trusted the
        index without checking a fingerprint, and this is where that check
        happens. After it, a source is re-examined only once its own ``refresh``
        interval has elapsed, and a config that declares no interval anywhere
        stops here -- nothing asked to be watched.
        """
        await self._pass()
        while self.config.min_refresh_seconds is not None:
            await asyncio.sleep(TICK_SECONDS)
            due = self._due()
            if due:
                await self._pass(only=due)

    async def _pass(self, **kwargs) -> None:
        """One refresh, whose failure is logged and never ends the loop.

        A background task that dies takes the whole refresh with it and the
        server goes on serving a frozen catalogue while looking healthy. That
        silence is the failure mode this exists to prevent, so every exception
        is caught here and the loop goes round again.
        """
        try:
            rebuilt = await self.refresh_async(**kwargs)
        except Exception:
            log.exception("refresh pass failed")
        else:
            if rebuilt:
                log.info(
                    "rebuilt %s; now at generation %d",
                    ", ".join(rebuilt),
                    self.generation,
                )

    def _due(self) -> list[str]:
        """The sources whose own refresh interval has elapsed since they were
        last *checked* -- not since their record was last *built*.

        Those differ the moment a check finds nothing to rebuild: `built` stays
        put, but the source has still been examined and must not come due
        again until its own interval passes a second time. A source never
        checked (``-inf``) is due immediately.
        """
        now = time.monotonic()
        return [
            source.name
            for source in self.config.sources
            if source.refresh_seconds is not None
            and now - self._checked.get(source.name, float("-inf"))
            >= source.refresh_seconds
        ]

    def run(
        self, transport: str = "http", host: str = "0.0.0.0", port: int = 8000
    ) -> None:
        """Serve on ``transport``, blocking until the process is stopped."""
        if transport == "stdio":
            self.mcp.run(transport="stdio")
        else:
            self.mcp.run(transport="http", host=host, port=port)


def _can_remember() -> bool:
    """Whether this connection is worth trying to remember state against.

    Announcing once means remembering what was announced, and the only place to
    remember it is state keyed by the session. An HTTP request carrying no
    ``mcp-session-id`` header has no session to key on, so it is never worth
    trying: False.

    Anything that is not an HTTP request -- stdio, in-memory -- has no such
    header to check, so this defaults to True for it. That default is
    optimistic, not a guarantee: under FastMCP 4.0.3 neither actually keeps one
    continuous session either -- ``test_a_sessionless_connection_is_told_...``
    shows the real in-memory transport minting a fresh state key per request,
    the same as a sessionless HTTP client. The state calls this makes for them
    are therefore wasted, not merely redundant, but harmless: ``AnnounceChanges``
    already guards every one of them against a state store that will not read
    them back.
    """
    http = http_request()
    if http is None:
        return True
    _, headers = http
    return bool(headers.get(SESSION_HEADER))


def _same_failure(old: SourceRecord | None, new: SourceRecord) -> bool:
    """Whether a rebuild produced the same failure the record already carried.

    A source that is still missing has not *changed*, and counting it as a
    rebuild would advance the generation on every single pass -- announcing a
    new catalogue to every client, forever, because one directory is absent.
    """
    return (
        old is not None
        and old.status == "failed"
        and new.status == "failed"
        and old.error == new.error
    )
