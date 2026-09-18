"""Plugins: what the config declares and a marketplace publishes, resolved.

A ``Plugin`` is the unit a library selects and a catalogue harvests. A
``Fetch`` is the tree it lives in -- one URL at one ref, materialised once
however many plugins read it -- and the two are separate because a
marketplace's seven entries are usually seven plugins in *one* repository:
resolving them into one ``Fetch`` is what stops seven clones of it.

Nothing here imports FastMCP, and nothing here resolves a credential: a
``Fetch`` carries the ``BasicAuth`` *reference* its source declared, and
``sources/`` resolves it at the moment it connects.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..config import BUILTIN_SCHEMES
from .address import Address, parse_address

if TYPE_CHECKING:
    from ..config import BasicAuth, Config, LibraryConfig, PluginConfig


@dataclass(frozen=True)
class Fetch:
    """One URL at one ref, materialised once into the cache, however many
    plugins read it."""

    key: str
    backend: str
    url: str
    ref: str | None = None
    auth: BasicAuth | None = None
    cache: str = "snapshot"
    refresh_seconds: int | None = None

    @property
    def slug(self) -> str:
        """This fetch's place in the cache: readable, then a digest.

        The readable half is for whoever is looking at the cache directory; the
        digest is what makes it unique, since the readable half is truncated
        and two long keys can flatten onto one prefix. Per ref, because two
        refs of one repository are two trees.
        """
        readable = re.sub(r"[^a-z0-9]+", "-", self.key.lower()).strip("-")[:60]
        return f"{readable}-{hashlib.sha256(self.key.encode()).hexdigest()[:8]}"

    @property
    def live(self) -> bool:
        return self.cache == "live"


@dataclass(frozen=True)
class Globs:
    """What a plugin serves, per kind, relative to its root.

    ``None`` for a kind means "nothing was said about it": the plugin's own
    manifest decides, and failing that the conventions. An empty tuple is a
    decision -- serve none of that kind.

    ``commands`` is not a kind and serves nothing: it is which of the
    ``prompts`` patterns came from a *command* declaration -- a manifest's
    ``commands`` or an entry's -- because that is a Claude signal and the file
    it selects may sit anywhere, penpot's ``prompts/`` included. A config's own
    ``prompts:`` glob is not a command declaration and leaves it empty.
    """

    skills: tuple[str, ...] | None = None
    prompts: tuple[str, ...] | None = None
    files: tuple[str, ...] | None = None
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plugin:
    """A resolved plugin: its metadata, the tree it lives in, and where in it."""

    id: str
    name: str
    description: str
    category: str | None
    tags: tuple[str, ...]
    keywords: tuple[str, ...]
    version: str | None
    fetch: Fetch
    subdir: str
    globs: Globs
    dialect: str = "auto"
    marketplace: str | None = None

    @property
    def labels(self) -> frozenset[str]:
        """What a selector and a scope match on.

        A marketplace entry carries ``tags`` and a ``plugin.json`` carries
        ``keywords``; they mean the same thing to whoever is searching, and
        which one a publisher used is not a distinction worth making here.
        """
        return frozenset(self.tags) | frozenset(self.keywords)


def fetch_for(config: Config, address: Address) -> Fetch:
    """The fetch an address resolves to, through the source that names it."""
    if address.scheme in BUILTIN_SCHEMES:
        return Fetch(
            key=address.key, backend="file", url=address.path, ref=address.ref
        )

    source = config.source(address.scheme)
    backend = source.backend
    if backend == "git":
        base = source.url.removeprefix("git+")
    elif backend == "webdav":
        base = source.url.removeprefix("webdav+")
    else:
        base = urlsplit(source.url).path
    return Fetch(
        key=address.key,
        backend=backend,
        url=f"{base.rstrip('/')}/{address.path}",
        ref=address.ref,
        auth=source.auth,
        cache=source.cache,
        refresh_seconds=source.refresh_seconds,
    )


def plugin_from_config(config: Config, plugin: PluginConfig) -> Plugin:
    """One declared plugin, resolved. Its id is its name: ours need no library."""
    address = plugin.address
    return Plugin(
        id=plugin.name,
        name=plugin.name,
        description=plugin.description,
        category=plugin.category,
        tags=tuple(plugin.tags),
        keywords=tuple(plugin.keywords),
        version=plugin.version,
        fetch=fetch_for(config, address),
        subdir=address.subdir,
        globs=Globs(
            skills=_tuple(plugin.skills),
            prompts=_tuple(plugin.prompts),
            files=_tuple(plugin.files),
        ),
        dialect=plugin.dialect,
    )


def declared_plugins(config: Config) -> list[Plugin]:
    """Every plugin the config declares, in the order it declares them."""
    return [plugin_from_config(config, plugin) for plugin in config.plugins]


def marketplace_fetches(
    config: Config,
) -> dict[str, tuple[LibraryConfig, Fetch, Address]]:
    """Each marketplace library, with the fetch and address its catalog is at.

    The address travels with the fetch because the marketplace's own subdir is
    what an entry's relative ``source`` is relative to.
    """
    found: dict[str, tuple[LibraryConfig, Fetch, Address]] = {}
    for library in config.libraries:
        if not library.source:
            continue
        address = parse_address(library.source)
        found[library.name] = (library, fetch_for(config, address), address)
    return found


def _tuple(patterns: list[str] | None) -> tuple[str, ...] | None:
    return None if patterns is None else tuple(patterns)
