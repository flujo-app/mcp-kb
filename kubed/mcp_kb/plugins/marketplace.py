"""A ``marketplace.json``: somebody else's catalog, read as plugins of ours.

A library whose ``source`` is a marketplace takes its shape from the catalog:
every entry becomes a plugin, with the entry's own category, tags and version,
and an id of ``<entry>@<library>`` so two catalogs may publish the same name.
Nothing about an entry is editable here -- to change one you declare your own
plugin against the same repository, which costs no second clone because a
fetch is keyed by URL and ref.

An entry this server cannot install is *skipped*, never guessed at: ``npm``
and ``archive`` because nothing here unpacks a package, and ``command`` because
nothing fetched is ever executed. A skip carries its reason so ``/health`` can
say why a plugin an operator expected is not there, and one bad entry never
costs the rest of the catalog.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..catalogue.harvest import readable
from ..config import NAME
from . import Fetch, Globs, Plugin, fetch_for
from .address import Address, parse_address
from .manifest import command_patterns, paths

if TYPE_CHECKING:
    from ..config import Config, LibraryConfig, SourceConfig

MARKETPLACE_FILES = (
    ".claude-plugin/marketplace.json",
    ".github/plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
)


@dataclass(frozen=True)
class Marketplace:
    name: str
    plugins: tuple[Plugin, ...]
    skipped: tuple[dict[str, str], ...]


class _Skip(ValueError):
    """This entry is not installable here, and the message says why."""


def find_marketplace(root: Path) -> Path | None:
    """The catalog file in a tree, in the order the conventions are published.

    Through ``readable``, the guard every read shares: ``is_file()`` follows a
    symlink, so a tree whose ``.claude-plugin/marketplace.json`` points out of
    itself would have a foreign catalogue read as its own. A link *inside* the
    tree still resolves, which is what makes a ConfigMap mount servable.
    """
    for name in MARKETPLACE_FILES:
        path = readable(root, name)
        if path is not None:
            return path
    return None


def read_marketplace(
    root: Path,
    *,
    library: LibraryConfig,
    fetch: Fetch,
    address: Address,
    config: Config,
) -> Marketplace:
    """The catalog at ``root``, as plugins of ``library``.

    ``address`` travels with ``fetch`` because an entry's relative source is
    relative to the *marketplace's* own subdirectory, not to the tree root.
    """
    path = find_marketplace(root)
    if path is None:
        looked = ", ".join(MARKETPLACE_FILES)
        raise ValueError(f"no marketplace found under {root}: looked for {looked}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise ValueError(f"{path.name} must be an object holding a plugins list")

    plugins: list[Plugin] = []
    skipped: list[dict[str, str]] = []
    for raw in data["plugins"]:
        try:
            plugins.append(
                _plugin(
                    raw, library=library, fetch=fetch, address=address, config=config
                )
            )
        except _Skip as skip:
            skipped.append({"plugin": _label(raw), "reason": str(skip)})

    return Marketplace(
        name=_text(data.get("name")) or library.name,
        plugins=tuple(plugins),
        skipped=tuple(skipped),
    )


def _plugin(
    raw: object,
    *,
    library: LibraryConfig,
    fetch: Fetch,
    address: Address,
    config: Config,
) -> Plugin:
    if not isinstance(raw, dict):
        raise _Skip("an entry must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not re.match(NAME, name):
        raise _Skip(f"{name!r} is not a plugin name")
    if raw.get("strict") is False:
        # The entry would be the whole definition, the plugin's own manifest
        # ignored. Skipped until something we pull actually publishes one.
        raise _Skip("strict: false entries are not supported")

    where, subdir = _where(
        raw.get("source"), fetch=fetch, address=address, config=config
    )
    return Plugin(
        id=f"{name}@{library.name}",
        name=name,
        description=_text(raw.get("description")) or "",
        category=_text(raw.get("category")),
        tags=_strings(raw.get("tags")),
        keywords=_strings(raw.get("keywords")),
        version=_text(raw.get("version")),
        fetch=where,
        subdir=subdir,
        globs=_globs(raw),
        marketplace=library.name,
    )


def _where(
    source: object, *, fetch: Fetch, address: Address, config: Config
) -> tuple[Fetch, str]:
    """The fetch and subdir one entry's ``source`` names.

    A relative path shares the marketplace's own fetch -- the common case, and
    what makes a seven-entry catalog one clone. Every other form names a
    repository, which has to be one of *our* declared sources: a catalog does
    not get to introduce a host, because a host is where credentials and a
    refresh interval are configured.
    """
    if isinstance(source, str):
        return fetch, _join(address.subdir, source)
    if not isinstance(source, dict) or not isinstance(source.get("source"), str):
        raise _Skip("an entry needs a source")

    kind = source["source"]
    ref = source.get("sha") or source.get("ref")  # a sha is the pinned one
    if kind == "github":
        repo = source.get("repo")
        if not isinstance(repo, str) or not repo:
            raise _Skip("a github entry needs a repo")
        declared = _declared(config, "https://github.com")
        if declared is None:
            raise _Skip("no source for github.com is declared")
        return _fetch(config, declared, repo, ref), ""
    if kind in ("url", "git-subdir"):
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise _Skip(f"a {kind} entry needs a url")
        found = _under(config, url)
        if found is None:
            raise _Skip(f"no source for {urlsplit(url).netloc} is declared")
        declared, path = found
        subdir = ""
        if kind == "git-subdir":
            path_in_repo = source.get("path")
            if not isinstance(path_in_repo, str) or not path_in_repo.strip():
                # Falling back to the repository root would serve the whole
                # thing as one plugin, which is a guess, not a default.
                raise _Skip("a git-subdir entry needs a path")
            subdir = _join("", path_in_repo)
        return _fetch(config, declared, path, ref), subdir
    raise _Skip(f"source type {kind} is not supported")


def _declared(config: Config, url: str) -> SourceConfig | None:
    """The declared git source that *is* this host, e.g. github.com."""
    for source in config.sources:
        if source.backend == "git" and _base(source) == url:
            return source
    return None


def _under(config: Config, url: str) -> tuple[SourceConfig, str] | None:
    """The declared git source this URL sits under, and the path below it.

    A trailing ``.git`` goes: it is a spelling of the same repository, and the
    path is what the fetch key is built from -- so a catalogue writing
    ``https://github.com/o/r.git`` shares its clone with a plugin somebody
    declared as ``github://o/r``, instead of cloning it a second time and
    reporting a second fetch in ``/health``.
    """
    for source in config.sources:
        base = _base(source)
        if source.backend == "git" and url.startswith(f"{base}/"):
            return source, url[len(base) + 1 :].removesuffix(".git")
    return None


def _base(source: SourceConfig) -> str:
    return source.url.removeprefix("git+").rstrip("/")


def _fetch(
    config: Config, source: SourceConfig, path: str, ref: object
) -> Fetch:
    """The fetch for a repository under a declared source, through the grammar.

    Spelled as an address and parsed rather than built, so a path out of a
    fetched catalog meets the same refusals a configured one does -- and so it
    keys identically to a plugin somebody declared against the same repo.
    """
    text = f"{source.name}://{path}"
    if isinstance(ref, str) and ref:
        text += f"?ref={ref}"
    try:
        return fetch_for(config, parse_address(text))
    except ValueError as exc:
        raise _Skip(str(exc)) from exc


def _join(subdir: str, relative: str) -> str:
    """``relative`` resolved under the marketplace's own subdirectory."""
    if relative.startswith("/"):
        raise _Skip(f"source {relative!r} is not a relative path")
    base = [p for p in PurePosixPath(subdir).parts if p != "."]
    parts = list(base)
    for part in PurePosixPath(relative.strip()).parts:
        if part == "..":
            if len(parts) <= len(base):
                raise _Skip(f"source {relative!r} escapes the marketplace")
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def _globs(raw: dict) -> Globs:
    """The components an entry lists itself, and None for the ones it does not.

    Grafana's entries are what this is for: one repository, seven plugins, each
    naming its own skills -- which is how its root ``template`` skill stays out
    of the catalogue without a hand-written glob here.
    """
    try:
        skills = paths(raw.get("skills"), "skills")
        commands = command_patterns(raw.get("commands"))
    except ValueError as exc:
        raise _Skip(str(exc)) from exc
    # The commands twice: what to serve as prompts, and the note that the entry
    # called them commands -- which is what makes them Claude's wherever in the
    # tree they sit.
    return Globs(skills=skills, prompts=commands, commands=commands or ())


def _label(raw: object) -> str:
    name = raw.get("name") if isinstance(raw, dict) else None
    return name if isinstance(name, str) and name else "?"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()
