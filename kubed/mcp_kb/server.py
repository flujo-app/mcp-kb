"""The MCP server itself: wiring, and the lifecycle of what it serves.

Assembles a FastMCP instance from the catalogue a config file describes -- the
address space, the resources, the mirror tools, the routes -- and runs it on a
transport. Turning a config source into a directory is ``sources``'s job;
deciding what in it counts as a skill, a prompt or a library-level file is
``catalogue/harvest.py``'s; building one immutable view of all of it is
``catalogue/snapshot.py``'s; the catalogue itself lives in
``catalogue/skills.py``, ``catalogue/prompts.py`` and ``catalogue/uris.py``; and
deciding when a source is looked at again is ``catalogue/refresh.py``'s. This
module walks the config in source order, connects the pieces, and owns the
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

from fastmcp import FastMCP

from . import routes
from .catalogue.index import INDEX_VERSION, Index, SourceRecord, config_hash, now
from .catalogue.prompts import FilePrompt
from .catalogue.refresh import Schedule, keep_last_good, loop, moved, same_failure
from .catalogue.skills import LibraryFiles, SkillIndex
from .catalogue.snapshot import (
    Snapshot,
    build_snapshot,
    build_source,
    log_changes,
    record_from_index,
)
from .catalogue.uris import Catalogue
from .config import Config
from .mcp import prompts, resources, tools
from .mcp.announce import AnnounceChanges
from .mcp.request import client_reads_resources, client_uses_prompts

log = logging.getLogger(__name__)

INDEX_FILE = "index.json"

INSTRUCTIONS = """\
This server hosts Agent Skills: instruction packages that teach you how to \
perform a specific task. Everything it serves is a `skill://` URI, and reading \
one is the only operation there is.

Work down the address space, cheapest first. Listing gives you indexes -- one \
per library (`skill://grafana/_index.md`), one per folder of skills within a \
library (`skill://grafana/grafana-lgtm/_index.md`). Reading an index gives you \
what is directly in it, as URIs: its folders' indexes, and its skills. \
Reading a skill's URI (`skill://grafana/grafana-lgtm/loki/SKILL.md`) gives you \
the instructions to follow.

A skill may ship supporting files. Read `_manifest` in place of `SKILL.md` to \
list them, then read one by its path under the same skill. A library may also \
ship files outside its skills, listed at `skill://<library>/_files.md`; a path \
a skill cites that is not inside the skill is usually one of those. Do not \
read files you have no use for -- a skill citing one is not a reason to fetch \
it. Only files are read: a library, a folder or a skill's own directory serves \
nothing.
"""


class KnowledgeBase:
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
        # When each source was last examined; see catalogue/refresh.py, which
        # decides what that means and when one comes due again.
        self.schedule = Schedule()

        self._records = self._cold_start()
        self.snapshot: Snapshot = build_snapshot(config, self._records, generation=0)
        log_changes({}, self.snapshot.status)
        self._write_index()

        ttl = config.min_refresh_seconds
        self.mcp = FastMCP(
            "mcp-kb",
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
        resource_tools = tools.register(self.mcp, lambda: self.snapshot.catalogue)
        prompt_tools = prompts.register(self.mcp, lambda: self.snapshot)
        mirrors = dict.fromkeys(resource_tools, client_reads_resources)
        mirrors.update(dict.fromkeys(prompt_tools, client_uses_prompts))
        self.mcp.add_middleware(resources.HideMirrorTools(mirrors))
        self.mcp.add_middleware(AnnounceChanges(self))
        routes.register(self.mcp, self)

    # -- what the snapshot currently holds -----------------------------------

    @property
    def generation(self) -> int:
        return self.snapshot.generation

    @property
    def index(self) -> SkillIndex:
        return self.snapshot.index

    @property
    def resources(self) -> LibraryFiles:
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

        A source built here whose server refuses its credentials stops the
        process: ``AccessRefused`` escapes, and ``main`` exits naming it. A
        refusal is the one failure a wait does not fix -- a wrong password, an
        account not yet let in -- and a pod that exits is restarted, visibly,
        until it is fixed, where one that serves on looks healthy with a
        library missing. Every other failure stays a failed source, so an
        unreachable remote at boot never takes the libraries that did load down
        with it; and a refusal on a later refresh, of a source already serving,
        makes it stale and is logged.

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
                self.config, source, self.cache, refusal_is_fatal=True
            )
        return records

    def _write_index(self) -> None:
        """Persist the current records, or log why the next boot will be slow.

        A cache directory that cannot be written is not fatal: the catalogue is
        already built and serving. It only means the next cold start harvests
        everything again, which is the cost the index exists to save.
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
                # Examined, whether or not it turns out stale -- the schedule
                # runs off this, so it must move on every check and not only
                # on a rebuild.
                self.schedule.examined(source.name, checked_at)
                if (
                    not force
                    and current is not None
                    and not moved(source, self.cache, current)
                ):
                    continue
                built = build_source(self.config, source, self.cache)
                fresh = keep_last_good(current, built)
                if same_failure(current, fresh):
                    continue
                records[source.name] = fresh
                rebuilt.append(source.name)

            if not rebuilt:
                return []
            snapshot = build_snapshot(
                self.config, records, generation=self.snapshot.generation + 1
            )
            self._records = records
            log_changes(self.snapshot.status, snapshot.status)
            self.snapshot = snapshot
            # Best-effort and therefore last: a raise here must not leave
            # _records advanced while snapshot still holds the old generation.
            self._write_index()
            return rebuilt

    async def refresh_async(self, **kwargs) -> list[str]:
        """``refresh`` off the event loop, because all of it blocks."""
        return await asyncio.to_thread(self.refresh, **kwargs)

    # -- keeping it current --------------------------------------------------

    @contextlib.asynccontextmanager
    async def _lifespan(self, server: FastMCP) -> AsyncIterator[dict]:
        """Run the refresh loop for as long as the server is up."""
        del server
        task = asyncio.create_task(loop(self))
        try:
            yield {}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def run(
        self, transport: str = "http", host: str = "0.0.0.0", port: int = 8000
    ) -> None:
        """Serve on ``transport``, blocking until the process is stopped.

        FastMCP's banner is a box of box-drawing characters, which reads well in
        a terminal and as noise in a log collector; its own "Starting MCP
        server" line says the same. uvicorn is given no log config of its own,
        so its lines go through the handler ``main.configure_logging``
        installed and read like the rest.
        """
        if transport == "stdio":
            self.mcp.run(transport="stdio", show_banner=False)
        else:
            self.mcp.run(
                transport="http",
                host=host,
                port=port,
                show_banner=False,
                uvicorn_config={"log_config": None},
            )
