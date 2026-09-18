"""The MCP server itself: wiring, and the lifecycle of what it serves.

Assembles a FastMCP instance from the catalogue a config file describes -- the
address space, the resources, the mirror tools, the routes -- and runs it on a
transport. Turning a fetch into a directory is ``sources``'s job; resolving
the config's plugins and reading a marketplace is ``plugins``'s; deciding what
in a plugin counts as a skill, a prompt or a library-level file is
``catalogue/harvest.py``'s; building one immutable view of all of it is
``catalogue/snapshot.py``'s; the catalogue itself lives in
``catalogue/skills.py``, ``catalogue/prompts/`` and ``catalogue/uris.py``; and
deciding when a fetch is looked at again is ``catalogue/refresh.py``'s. This
module runs the resolution phases in order, connects the pieces, and owns the
one mutable thing in the process: which ``Snapshot`` is current.

Three rules follow from that, and every change here has to keep them:

- **Start from the index, not from the sources.** ``<cache>/index.json`` holds
  what each fetch and each plugin yielded last time. If it was built from this
  same config, boot reuses every record whose tree is still on disk and
  rebuilds only the rest. A pod restart is then milliseconds and touches no
  network, and the question "has anything changed?" is deferred to the
  background pass that runs a moment later.
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
from .catalogue.index import (
    INDEX_VERSION,
    FetchRecord,
    Index,
    PluginRecord,
    config_hash,
    now,
)
from .catalogue.prompts import FilePrompt
from .catalogue.refresh import Schedule, keep_last_good, loop, moved, same_failure
from .catalogue.skills import LibraryFiles, SkillIndex
from .catalogue.snapshot import (
    SERVABLE,
    Library,
    Snapshot,
    assemble_libraries,
    build_fetch,
    build_plugin,
    build_snapshot,
    failed_plugin,
    log_changes,
    unrooted,
    with_manifest,
)
from .catalogue.uris import Catalogue
from .config import Config
from .mcp import prompts, resources, tools
from .mcp.announce import AnnounceChanges
from .mcp.pins import RefuseEmptyScope, what_is_wrong
from .mcp.request import client_reads_resources, client_uses_prompts
from .plugins import Fetch, Plugin, declared_plugins, marketplace_fetches
from .plugins.marketplace import find_marketplace, read_marketplace

log = logging.getLogger(__name__)

INDEX_FILE = "index.json"

INSTRUCTIONS = """\
This server hosts Agent Skills: instruction packages that teach you how to \
perform a specific task. Everything it serves is a `skill://` URI, and reading \
one is the only operation there is.

Work down the address space, cheapest first. Listing gives you indexes -- one \
per library (`skill://grafana/_index.md`), one per top-level folder of a \
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
    """An MCP server over the libraries a config file names.

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
        # Every write of the records and self.snapshot happens under this, so
        # two refreshes triggered at once still swap in a single order.
        self._lock = threading.Lock()
        # When each fetch was last examined; see catalogue/refresh.py, which
        # decides what that means and when one comes due again.
        self.schedule = Schedule()

        self._fetches: dict[str, FetchRecord] = {}
        self._records: dict[str, PluginRecord] = {}
        self._plugins: list[Plugin] = []
        self._libraries: list[Library] = []
        self._cold_start()
        self.snapshot: Snapshot = self._build(generation=0)
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
        self.mcp.add_middleware(
            RefuseEmptyScope(lambda scope: what_is_wrong(scope, config, self.snapshot))
        )
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

    @property
    def fetches(self) -> list[Fetch]:
        """This generation's fetches: the marketplaces', then every plugin's.

        Derived from the plugins rather than stored, so a marketplace that
        gained an entry on another repository brings that repository's fetch
        into the next pass by itself.
        """
        found: dict[str, Fetch] = {}
        for _, fetch, _ in marketplace_fetches(self.config).values():
            found.setdefault(fetch.key, fetch)
        for plugin in self._plugins:
            found.setdefault(plugin.fetch.key, plugin.fetch)
        return list(found.values())

    # -- building it ---------------------------------------------------------

    def _cold_start(self) -> None:
        """Resolve everything, reusing the on-disk index wherever it holds.

        A fetch built here whose server refuses its credentials stops the
        process: ``AccessRefused`` escapes, and ``main`` exits naming it. A
        refusal is the one failure a wait does not fix -- a wrong password, an
        account not yet let in -- and a pod that exits is restarted, visibly,
        until it is fixed, where one that serves on looks healthy with a
        library missing. Every other failure stays a failed fetch, so an
        unreachable remote at boot never takes the libraries that did load down
        with it; and a refusal on a later refresh, of a fetch already serving,
        makes it stale and is logged.

        No fingerprint is taken here. Checking one means walking every tree,
        which is the cost this whole file exists to move off the boot path;
        the verification pass the lifespan runs immediately afterwards does it
        instead, and until it finishes the server serves last boot's answer --
        which is almost always still the right one.
        """
        stored = Index.read(self.index_path)
        fetches: dict[str, FetchRecord] = {}
        records: dict[str, PluginRecord] = {}
        if stored is not None and stored.config_hash == config_hash(self.config):
            fetches = {
                key: record
                for key, record in stored.fetches.items()
                if _serving(record)
            }
            records = {
                pid: record
                for pid, record in stored.plugins.items()
                if record.fetch in fetches and _serving(record)
            }
        elif stored is not None:
            log.info("config changed since %s was written; rebuilding", self.index_path)
        self._fetches = fetches
        self._plugins, self._libraries, self._records = self._assemble(
            fetches, records, refusal_is_fatal=True
        )

    def _assemble(
        self,
        fetches: dict[str, FetchRecord],
        reusable: dict[str, PluginRecord],
        *,
        refusal_is_fatal: bool = False,
    ) -> tuple[list[Plugin], list[Library], dict[str, PluginRecord]]:
        """The resolution phases: marketplaces, fetches, plugins, then libraries.

        ``fetches`` is the records already in hand -- reused from the index or
        left alone by a refresh -- and is *added to*: every fetch a marketplace
        or a plugin needs that is not in it is built here, and every record
        nothing needs any more is dropped, so on return it holds exactly this
        generation's fetches. ``reusable`` is the plugin records whose fetch was
        not rebuilt; a plugin not in it is harvested again, which is cheap and
        touches no network.

        A marketplace's plugins are re-derived by re-reading its
        ``marketplace.json`` off the tree on disk every time: cheap, and the
        only way a refresh of that fetch can serve an entry it gained.
        """
        declared = declared_plugins(self.config)
        markets = marketplace_fetches(self.config)

        derived: list[Plugin] = []
        errors: dict[str, str] = {}
        skipped: dict[str, tuple[dict[str, str], ...]] = {}
        for name, (library, fetch, address) in markets.items():
            record = self._fetch_record(fetches, fetch, refusal_is_fatal)
            if not _serving(record):
                errors[name] = record.error or "the marketplace could not be fetched"
                continue
            root = Path(record.root or "")
            if address.subdir:
                root = root / address.subdir
            if find_marketplace(root) is None:
                errors[name] = f"no marketplace.json under {address.subdir or '/'}"
                continue
            try:
                market = read_marketplace(
                    root,
                    library=library,
                    fetch=fetch,
                    address=address,
                    config=self.config,
                )
            except ValueError as exc:
                # An unreadable file's error quotes its absolute path, which
                # is a cache path: stripped, as every harvest string is.
                errors[name] = unrooted(str(exc), root)
                continue
            derived += market.plugins
            if market.skipped:
                skipped[name] = market.skipped

        plugins = [*declared, *derived]
        for plugin in plugins:
            self._fetch_record(fetches, plugin.fetch, refusal_is_fatal)
        needed = {p.fetch.key for p in plugins}
        needed |= {fetch.key for _, fetch, _ in markets.values()}
        for key in [key for key in fetches if key not in needed]:
            del fetches[key]

        # Each plugin comes back completed by its manifest, so the selectors,
        # the scopes and /health all see the labels the publisher declared.
        completed: list[Plugin] = []
        records: dict[str, PluginRecord] = {}
        for plugin in plugins:
            plugin, records[plugin.id] = self._plugin_record(
                plugin, fetches[plugin.fetch.key], reusable
            )
            completed.append(plugin)
        libraries = assemble_libraries(
            self.config, completed, errors=errors, skipped=skipped
        )
        return completed, libraries, records

    def _fetch_record(
        self, fetches: dict[str, FetchRecord], fetch: Fetch, refusal_is_fatal: bool
    ) -> FetchRecord:
        """The record for ``fetch``, built now if there is none in hand."""
        record = fetches.get(fetch.key)
        if record is None:
            record = build_fetch(fetch, self.cache, refusal_is_fatal=refusal_is_fatal)
            fetches[fetch.key] = record
        return record

    def _plugin_record(
        self, plugin: Plugin, fetch: FetchRecord, reusable: dict[str, PluginRecord]
    ) -> tuple[Plugin, PluginRecord]:
        """The plugin completed by its manifest, and its harvest: reused when
        it can be, rebuilt otherwise.

        The manifest is read whenever the root is there, reused record or not:
        it is one small file, and the labels it adds are not in the index.
        """
        if not _serving(fetch):
            return plugin, failed_plugin(plugin, fetch.error or "the fetch failed")
        root = Path(fetch.root or "")
        if plugin.subdir:
            root = root / plugin.subdir
        if not root.is_dir():
            return plugin, failed_plugin(
                plugin, f"{plugin.subdir or '/'} is not a directory in {fetch.key}"
            )
        try:
            plugin, globs = with_manifest(plugin, root)
        except ValueError as exc:
            # An OSError behind the read quotes the cache path; /health is
            # published, so the root comes back out of it here as everywhere.
            return plugin, failed_plugin(plugin, unrooted(str(exc), root))
        kept = reusable.get(plugin.id)
        if kept is not None and kept.fetch == fetch.key and _serving(kept):
            return plugin, kept
        return plugin, build_plugin(plugin, root, globs=globs)

    def _build(self, generation: int) -> Snapshot:
        return build_snapshot(
            self.config,
            self._plugins,
            self._libraries,
            self._fetches,
            self._records,
            generation,
        )

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
            fetches=self._fetches,
            plugins=self._records,
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

        Returns the fetch keys actually rebuilt, in this generation's order --
        empty when nothing moved, which is the common case and the reason the
        generation does not advance on every pass. ``only`` narrows the sweep
        to the fetches the caller already knows are due; ``force`` skips the
        fingerprint check and rebuilds them regardless.

        A rebuilt fetch re-harvests every plugin that reads it, and every
        marketplace on it is re-read, so an entry it gained is served; the
        plugins of untouched fetches keep their records.

        Synchronous and serialised: this walks trees, hashes files and writes
        ``index.json``, and two of them at once would race for the index's
        temporary file. Call it from the event loop through ``refresh_async``.
        """
        keys = None if only is None else set(only)
        checked_at = time.monotonic()
        with self._lock:
            fetches = dict(self._fetches)
            rebuilt: list[str] = []
            for fetch in self.fetches:
                if keys is not None and fetch.key not in keys:
                    continue
                current = fetches.get(fetch.key)
                # Examined, whether or not it turns out stale -- the schedule
                # runs off this, so it must move on every check and not only
                # on a rebuild.
                self.schedule.examined(fetch.key, checked_at)
                if (
                    not force
                    and current is not None
                    and not moved(fetch, self.cache, current)
                ):
                    continue
                built = build_fetch(fetch, self.cache)
                fresh = keep_last_good(current, built)
                if same_failure(current, fresh):
                    continue
                fetches[fetch.key] = fresh
                rebuilt.append(fetch.key)

            if not rebuilt:
                return []
            reusable = {
                pid: record
                for pid, record in self._records.items()
                if record.fetch not in rebuilt
            }
            plugins, libraries, records = self._assemble(fetches, reusable)
            self._fetches, self._plugins = fetches, plugins
            self._libraries, self._records = libraries, records
            snapshot = self._build(generation=self.snapshot.generation + 1)
            log_changes(self.snapshot.status, snapshot.status)
            self.snapshot = snapshot
            # Best-effort and therefore last: a raise here must not leave
            # the records advanced while snapshot still holds the old generation.
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


def _serving(record: FetchRecord | PluginRecord) -> bool:
    """Whether a record still names a tree on disk worth serving from.

    ``stale`` counts for a fetch -- its tree is the last good one -- and a
    plugin has no stale state, so for one this is simply ``ok``.
    """
    return (
        record.status in SERVABLE
        and record.root is not None
        and Path(record.root).is_dir()
    )
